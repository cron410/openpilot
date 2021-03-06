from cereal import car
from common.numpy_fast import clip, interp
from selfdrive.boardd.boardd import can_list_to_can_capnp
from selfdrive.car import apply_toyota_steer_torque_limits
from selfdrive.car import create_gas_command
from selfdrive.car.toyota.toyotacan import make_can_msg, create_video_target,\
                                           create_steer_command, create_ui_command, \
                                           create_ipas_steer_command, create_accel_command, \
                                           create_fcw_command
from selfdrive.car.toyota.values import ECU, STATIC_MSGS, TSSP2_CAR
from selfdrive.can.packer import CANPacker
from selfdrive.car.modules.ALCA_module import ALCAController
from selfdrive.phantom import Phantom

VisualAlert = car.CarControl.HUDControl.VisualAlert
AudibleAlert = car.CarControl.HUDControl.AudibleAlert

# Accel limits
ACCEL_HYST_GAP = 0.02  # don't change accel command for small oscilalitons within this value
ACCEL_MAX = 3.5  # 3.5 m/s2
ACCEL_MIN = -4.0 # 4   m/s2
ACCEL_SCALE = max(ACCEL_MAX, -ACCEL_MIN)

# Steer torque limits
class SteerLimitParams:
  STEER_MAX = 1500
  STEER_DELTA_UP = 10       # 1.5s time to peak torque
  STEER_DELTA_DOWN = 25     # always lower than 45 otherwise the Rav4 faults (Prius seems ok with 50)
  STEER_ERROR_MAX = 350     # max delta between torque cmd and torque motor

# Steer angle limits (tested at the Crows Landing track and considered ok)
ANGLE_MAX_BP = [0., 5.]
ANGLE_MAX_V = [510., 300.]
ANGLE_DELTA_BP = [0., 5., 15.]
ANGLE_DELTA_V = [5., .8, .15]     # windup limit
ANGLE_DELTA_VU = [5., 3.5, 0.4]   # unwind limit

# Blindspot codes
LEFT_BLINDSPOT = '\x41'
RIGHT_BLINDSPOT = '\x42'
BLINDSPOTDEBUG = True

TARGET_IDS = [0x340, 0x341, 0x342, 0x343, 0x344, 0x345,
              0x363, 0x364, 0x365, 0x370, 0x371, 0x372,
              0x373, 0x374, 0x375, 0x380, 0x381, 0x382,
              0x383]

def set_blindspot_debug_mode(lr,enable):
  if enable:
    m = lr + "\x02\x10\x60\x00\x00\x00\x00"
  else:
    m = lr + "\x02\x10\x01\x00\x00\x00\x00"
  return make_can_msg(1872, m, 0, False)

def poll_blindspot_status(lr):
  m = lr + "\x02\x21\x69\x00\x00\x00\x00"
  return make_can_msg(1872, m, 0, False)

def accel_hysteresis(accel, accel_steady, enabled):

  # for small accel oscillations within ACCEL_HYST_GAP, don't change the accel command
  if not enabled:
    # send 0 when disabled, otherwise acc faults
    accel_steady = 0.
  elif accel > accel_steady + ACCEL_HYST_GAP:
    accel_steady = accel - ACCEL_HYST_GAP
  elif accel < accel_steady - ACCEL_HYST_GAP:
    accel_steady = accel + ACCEL_HYST_GAP
  accel = accel_steady

  return accel, accel_steady


def process_hud_alert(hud_alert, audible_alert):
  # initialize to no alert
  steer = 0
  fcw = 0
  sound1 = 0
  sound2 = 0

  if hud_alert == VisualAlert.fcw:
    fcw = 1
  elif hud_alert == VisualAlert.steerRequired:
    steer = 1

  if audible_alert == AudibleAlert.chimeWarningRepeat:
    sound1 = 1
  elif audible_alert != AudibleAlert.none:
    # TODO: find a way to send single chimes
    sound2 = 1

  return steer, fcw, sound1, sound2


def ipas_state_transition(steer_angle_enabled, enabled, ipas_active, ipas_reset_counter):

  if enabled and not steer_angle_enabled:
    #ipas_reset_counter = max(0, ipas_reset_counter - 1)
    #if ipas_reset_counter == 0:
    #  steer_angle_enabled = True
    #else:
    #  steer_angle_enabled = False
    #return steer_angle_enabled, ipas_reset_counter
    return True, 0

  elif enabled and steer_angle_enabled:
    if steer_angle_enabled and not ipas_active:
      ipas_reset_counter += 1
    else:
      ipas_reset_counter = 0
    if ipas_reset_counter > 10:  # try every 0.1s
      steer_angle_enabled = False
    return steer_angle_enabled, ipas_reset_counter

  else:
    return False, 0


class CarController(object):
  def __init__(self, dbc_name, car_fingerprint, enable_camera, enable_dsu, enable_apg):
    self.braking = False
    # redundant safety check with the board
    self.controls_allowed = True
    self.last_steer = 0
    self.last_angle = 0
    self.accel_steady = 0.
    self.car_fingerprint = car_fingerprint
    self.alert_active = False
    self.last_standstill = False
    self.standstill_req = False
    self.angle_control = False
    self.blindspot_poll_counter = 0
    self.blindspot_blink_counter_left = 0
    self.blindspot_blink_counter_right = 0
    self.steer_angle_enabled = False
    self.ipas_reset_counter = 0
    self.last_fault_frame = -200
    self.blindspot_debug_enabled_left = False
    self.blindspot_debug_enabled_right = False
    self.phantom = Phantom()

    self.fake_ecus = set()
    if enable_camera: self.fake_ecus.add(ECU.CAM)
    if enable_dsu: self.fake_ecus.add(ECU.DSU)
    if enable_apg: self.fake_ecus.add(ECU.APGS)
    self.ALCA = ALCAController(self,True,False)  # Enabled True and SteerByAngle only False

    self.packer = CANPacker(dbc_name)

  def update(self, sendcan, enabled, CS, frame, actuators,
             pcm_cancel_cmd, hud_alert, audible_alert, forwarding_camera,
             left_line, right_line, lead, left_lane_depart, right_lane_depart):

    #update custom UI buttons and alerts
    CS.UE.update_custom_ui()
    if (frame % 1000 == 0):
      CS.cstm_btns.send_button_info()
      CS.UE.uiSetCarEvent(CS.cstm_btns.car_folder,CS.cstm_btns.car_name)
      
    # *** compute control surfaces ***

    # gas and brake

    apply_gas = clip(actuators.gas, 0., 1.)

    if CS.CP.enableGasInterceptor:
      # send only negative accel if interceptor is detected. otherwise, send the regular value
      # +0.06 offset to reduce ABS pump usage when OP is engaged
      apply_accel = 0.06 - actuators.brake
    else:
      apply_accel = actuators.gas - actuators.brake

    apply_accel, self.accel_steady = accel_hysteresis(apply_accel, self.accel_steady, enabled)
    apply_accel = clip(apply_accel * ACCEL_SCALE, ACCEL_MIN, ACCEL_MAX)
    # Get the angle from ALCA.
    alca_enabled = False
    alca_steer = 0.
    alca_angle = 0.
    turn_signal_needed = 0
    # Update ALCA status and custom button every 0.1 sec.
    if self.ALCA.pid == None and not CS.indi_toggle:
      self.ALCA.set_pid(CS)
    if (frame % 10 == 0):
      self.ALCA.update_status(CS.cstm_btns.get_button_status("alca") > 0)
    # steer torque
    alca_angle, alca_steer, alca_enabled, turn_signal_needed = self.ALCA.update(enabled, CS, frame, actuators)
    #apply_steer = int(round(alca_steer * STEER_MAX))

    self.phantom.update()
    # steer torque
    if self.phantom.data["status"]:
      apply_steer = int(round(self.phantom.data["angle"]))
      if abs(CS.angle_steers) > 400:
        apply_steer = 0
    else:
      apply_steer = int(round(alca_steer * SteerLimitParams.STEER_MAX))
      if abs(CS.angle_steers) > 100:
        apply_steer = 0
    if not CS.lane_departure_toggle_on:
      apply_steer = 0

    # only cut torque when steer state is a known fault
    if CS.steer_state in [3, 7, 9, 11, 25]:
      self.last_fault_frame = frame

    # Cut steering for 2s after fault
    cutout_time = 100 if self.phantom.data["status"] else 200

    if not enabled or (frame - self.last_fault_frame < cutout_time):
      apply_steer = 0
      apply_steer_req = 0
    else:
      apply_steer_req = 1

    apply_steer = apply_toyota_steer_torque_limits(apply_steer, self.last_steer, CS.steer_torque_motor, SteerLimitParams)
    if apply_steer == 0 and self.last_steer == 0:
      apply_steer_req = 0

    if not enabled and right_lane_depart and CS.v_ego > 12.5 and not CS.right_blinker_on:
      apply_steer = self.last_steer + 3
      apply_steer = min(apply_steer , 800)
      #print "right"
      #print apply_steer
      apply_steer_req = 1
      
    if not enabled and left_lane_depart and CS.v_ego > 12.5 and not CS.left_blinker_on:
      apply_steer = self.last_steer - 3
      apply_steer = max(apply_steer , -800)
      #print "left"
      #print apply_steer
      apply_steer_req = 1

    self.steer_angle_enabled, self.ipas_reset_counter = \
      ipas_state_transition(self.steer_angle_enabled, enabled, CS.ipas_active, self.ipas_reset_counter)
    #print("{0} {1} {2}".format(self.steer_angle_enabled, self.ipas_reset_counter, CS.ipas_active))

    # steer angle
    if self.steer_angle_enabled and CS.ipas_active:

      apply_angle = alca_angle
      #apply_angle = actuators.steerAngle
      angle_lim = interp(CS.v_ego, ANGLE_MAX_BP, ANGLE_MAX_V)
      apply_angle = clip(apply_angle, -angle_lim, angle_lim)

      # windup slower
      if self.last_angle * apply_angle > 0. and abs(apply_angle) > abs(self.last_angle):
        angle_rate_lim = interp(CS.v_ego, ANGLE_DELTA_BP, ANGLE_DELTA_V)
      else:
        angle_rate_lim = interp(CS.v_ego, ANGLE_DELTA_BP, ANGLE_DELTA_VU)

      apply_angle = clip(apply_angle, self.last_angle - angle_rate_lim, self.last_angle + angle_rate_lim)
    else:
      apply_angle = CS.angle_steers

    if not enabled and CS.pcm_acc_status:
      # send pcm acc cancel cmd if drive is disabled but pcm is still on, or if the system can't be activated
      pcm_cancel_cmd = 1

    # on entering standstill, send standstill request
    #if CS.standstill and not self.last_standstill:
    #  self.standstill_req = True
    if CS.pcm_acc_status != 8:
      # pcm entered standstill or it's disabled
      self.standstill_req = False

    self.last_steer = apply_steer
    self.last_angle = apply_angle
    self.last_accel = apply_accel
    self.last_standstill = CS.standstill

    can_sends = []

# Enable blindspot debug mode once
    if BLINDSPOTDEBUG:
      self.blindspot_poll_counter += 1
    if self.blindspot_poll_counter > 1000: # 10 seconds after start
      if CS.left_blinker_on:
        self.blindspot_blink_counter_left += 1
        #print "debug Left Blinker on"
      elif CS.right_blinker_on:
        self.blindspot_blink_counter_right += 1
      else:
        self.blindspot_blink_counter_left = 0
        self.blindspot_blink_counter_right = 0
        #print "debug Left Blinker off"
        if self.blindspot_debug_enabled_left:
          can_sends.append(set_blindspot_debug_mode(LEFT_BLINDSPOT, False))
          self.blindspot_debug_enabled_left = False
          #print "debug Left blindspot debug disabled"
        if self.blindspot_debug_enabled_right:
          can_sends.append(set_blindspot_debug_mode(RIGHT_BLINDSPOT, False))
          self.blindspot_debug_enabled_right = False
          #print "debug Right blindspot debug disabled"
      if self.blindspot_blink_counter_left > 9 and not self.blindspot_debug_enabled_left: #check blinds
        can_sends.append(set_blindspot_debug_mode(LEFT_BLINDSPOT, True))
        #print "debug Left blindspot debug enabled"
        self.blindspot_debug_enabled_left = True
      if self.blindspot_blink_counter_right > 5 and not self.blindspot_debug_enabled_right: #enable blindspot debug mode
        if CS.v_ego > 6: #polling at low speeds switches camera off
          can_sends.append(set_blindspot_debug_mode(RIGHT_BLINDSPOT, True))
          #print "debug Right blindspot debug enabled"
          self.blindspot_debug_enabled_right = True
    if self.blindspot_debug_enabled_left:
      if self.blindspot_poll_counter % 20 == 0 and self.blindspot_poll_counter > 1001:  # Poll blindspots at 5 Hz
        can_sends.append(poll_blindspot_status(LEFT_BLINDSPOT))
    if self.blindspot_debug_enabled_right:
      if self.blindspot_poll_counter % 20 == 10 and self.blindspot_poll_counter > 1005:  # Poll blindspots at 5 Hz
        can_sends.append(poll_blindspot_status(RIGHT_BLINDSPOT))

    #*** control msgs ***
    #print("steer {0} {1} {2} {3}".format(apply_steer, min_lim, max_lim, CS.steer_torque_motor)

    # toyota can trace shows this message at 42Hz, with counter adding alternatively 1 and 2;
    # sending it at 100Hz seem to allow a higher rate limit, as the rate limit seems imposed
    # on consecutive messages
    if ECU.CAM in self.fake_ecus:
      if self.angle_control:
        can_sends.append(create_steer_command(self.packer, 0., 0, frame))
      else:
        can_sends.append(create_steer_command(self.packer, apply_steer, apply_steer_req, frame))

    if self.angle_control:
      can_sends.append(create_ipas_steer_command(self.packer, apply_angle, self.steer_angle_enabled,
                                                   ECU.APGS in self.fake_ecus))
    elif ECU.APGS in self.fake_ecus:
      can_sends.append(create_ipas_steer_command(self.packer, 0, 0, True))
    
    
    if CS.cstm_btns.get_button_status("tr") > 0:
      distance = 1 
    else:
      distance = 0 
 
    # accel cmd comes from DSU, but we can spam can to cancel the system even if we are using lat only control
    if (frame % 3 == 0 and ECU.DSU in self.fake_ecus) or (pcm_cancel_cmd and ECU.CAM in self.fake_ecus):
      lead = lead or CS.v_ego < 12.    # at low speed we always assume the lead is present do ACC can be engaged
      if ECU.DSU in self.fake_ecus:
        can_sends.append(create_accel_command(self.packer, apply_accel, pcm_cancel_cmd, self.standstill_req, lead, distance))
      else:
        can_sends.append(create_accel_command(self.packer, 0, pcm_cancel_cmd, False, lead, distance))

    if (frame % 2 == 0) and (CS.CP.enableGasInterceptor):
        # send exactly zero if apply_gas is zero. Interceptor will send the max between read value and apply_gas.
        # This prevents unexpected pedal range rescaling
        can_sends.append(create_gas_command(self.packer, apply_gas, frame//2))

    if frame % 10 == 0 and ECU.CAM in self.fake_ecus and not forwarding_camera:
      for addr in TARGET_IDS:
        can_sends.append(create_video_target(frame//10, addr))

    # ui mesg is at 100Hz but we send asap if:
    # - there is something to display
    # - there is something to stop displaying
    alert_out = process_hud_alert(hud_alert, audible_alert)
    steer, fcw, sound1, sound2 = alert_out

    if (any(alert_out) and not self.alert_active) or \
       (not any(alert_out) and self.alert_active):
      send_ui = True
      self.alert_active = not self.alert_active
    else:
      send_ui = False
    if (frame % 100 == 0 or send_ui) and ECU.CAM in self.fake_ecus:
      can_sends.append(create_ui_command(self.packer, steer, sound1, sound2, left_line, right_line, left_lane_depart, right_lane_depart))

    if frame % 100 == 0 and ECU.DSU in self.fake_ecus and self.car_fingerprint not in TSSP2_CAR:
      can_sends.append(create_fcw_command(self.packer, fcw))

    #*** static msgs ***

    for (addr, ecu, cars, bus, fr_step, vl) in STATIC_MSGS:
      if frame % fr_step == 0 and ecu in self.fake_ecus and self.car_fingerprint in cars and not (ecu == ECU.CAM and forwarding_camera):
        # special cases
        if fr_step == 5 and ecu == ECU.CAM and bus == 1:
          cnt = (((frame // 5) % 7) + 1) << 5
          vl = chr(cnt) + vl
        elif addr in (0x489, 0x48a) and bus == 0:
          # add counter for those 2 messages (last 4 bits)
          cnt = ((frame // 100) % 0xf) + 1
          if addr == 0x48a:
            # 0x48a has a 8 preceding the counter
            cnt += 1 << 7
          vl += chr(cnt)

        can_sends.append(make_can_msg(addr, vl, bus, False))


    sendcan.send(can_list_to_can_capnp(can_sends, msgtype='sendcan'))
