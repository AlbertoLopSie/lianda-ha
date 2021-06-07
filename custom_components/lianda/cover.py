"""
 Copyright 2018 Alberto Lopez
 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

 http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.

 Modified from https://github.com/home-assistant/home-assistant/blob/dev/homeassistant/components/switch/broadlink.py

 This component supports the Lianda/Tube roller motors using a
 Broadlink RM2(Pro) as the 433 Mhz gateway to send the RF packets

 The Lianda RF protocol uses a rolling code per remote that needs to be
 incremented on every packet and is stored on a SQLite database named 'lianda'

 You can use the learnt remote codes from a physical remote (not recomended) or
 new remote ids created and programmed into the motors using the lianda_add_remote
 service (see roller motor user´s manual) Each cover/blind needs a unique remoteId

"""


# import datetime as dt
from threading import Timer
from datetime import timedelta
# from base64 import b64encode, b64decode
from homeassistant.util import dt as dt_util
from homeassistant.helpers.event import track_time_interval
# import asyncio
import binascii
import logging
import socket
import sqlite3

import voluptuous as vol

# from homeassistant.util.dt import utcnow
# from homeassistant.util import Throttle
from homeassistant.components.cover import (
    DOMAIN, CoverEntity, PLATFORM_SCHEMA, SUPPORT_OPEN, SUPPORT_CLOSE,
    SUPPORT_STOP, SUPPORT_SET_POSITION, ATTR_POSITION,
    ENTITY_ID_FORMAT)

from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_FRIENDLY_NAME,
    CONF_COVERS, CONF_TIMEOUT,
    CONF_HOST, CONF_MAC,
    STATE_UNKNOWN, STATE_CLOSED,  STATE_OPEN,
    STATE_CLOSING, STATE_OPENING)
import homeassistant.helpers.config_validation as cv

# REQUIREMENTS = ['broadlink==0.9.0']
REQUIREMENTS = ['broadlink==0.17.0']

_LOGGER = logging.getLogger(__name__)

# TIME_BETWEEN_UPDATES = timedelta(seconds=5)

# DOMAIN = 'cover'
DOMAIN = 'lianda'
DEFAULT_NAME = 'Lianda-Broadlink cover'

SERVICE_SEND_CMND = 'send_command'
SERVICE_RESET_RCODE = 'reset_rolling_code'
SERVICE_REMOTE_ADD = 'add_remote'
SERVICE_SET_POS = 'set_position'
SERVICE_SET_POS_REL = 'set_position_rel'
SERVICE_SYNC_POS = 'sync_position'

STATE_STOPPED = 'stopped'

DEFAULT_TIMEOUT = 10
DFLT_BROADLINK_SEND_RETRIES = 2
MAX_LIANDA_REPEATS = 8  # If the repeat count of the packet is greater than
# this Value the transmisssion is splitted into two different broadlink
# packets, first one carries the HW Sync packet and the remaining repeats
# are sent using the repeat feature of the Broadlink device
# The reason for this is an apparent limit on the Broadlink
# send_data function that seems to fail if the packet is
# over than 900 bytes long

POSITION_RESOLUTION = 4
ASSUMED_START_STATE = STATE_UNKNOWN
ASSUMED_START_POSITION = 50
MAX_POSITION_ERROR = 25
#SCAN_INTERVAL = timedelta(milliseconds=100)


# Enter a Sync Position Cycle if SYNC_REPEATS_KEYS consecutive UP/DOWN
# keypresses separated for no more than SYNC_REPEATS_DELAY milliseconds
# are made
SYNC_REPEATS_DELAY = 1000      # in  Milliseconds
SYNC_REPEATS_KEYS = 3

CONF_REMOTE_ID = "remote_id"
CONF_START_ROLLING_CODE = "start_rolling_code"
CONF_FULL_OPEN_TIME = "full_open_time"
CONF_FULL_CLOSE_TIME = "full_close_time"
CONF_COMMAND_REPEATS = "lianda_command_repeats"
CONF_BROADLINK_SEND_RETRIES = "broadlink_send_retries"


ATTR_COMMAND = "command"
ATTR_COMMAND_REPEATS = "repeats"
ATTR_PACKET_REPEATS = "broadlink_repeats"
ATTR_SYNC_FULLY_OPEN = "sync_fully_open"


DFLT_START_ROLLING_CODE = 1
DFLT_FULL_OPEN_TIME = 0
DFLT_FULL_CLOSE_TIME = 0
DFLT_COMMAND_REPEATS = 2
DFLT_PACKET_REPEATS = 0
PROG_COMMAND_REPEATS = 30   # Needs at least 33 repeats

LIANDA_CMD_STOP = 5
LIANDA_CMD_UP = 6
LIANDA_CMD_UP_STOP = 7
LIANDA_CMD_DOWN = 8
LIANDA_CMD_PROG = 12
LIANDA_CMD_DOWN_STOP = 9
LIANDA_CMD_UP_DOWN = 10
LIANDA_CMD_UP_DOWN_STOP = 11

BUTTON_MAP = {
    'u':            LIANDA_CMD_UP,
    'up':           LIANDA_CMD_UP,
    'u':            LIANDA_CMD_UP,
    'd':            LIANDA_CMD_DOWN,
    'down':         LIANDA_CMD_DOWN,
    's':            LIANDA_CMD_STOP,
    'stop':         LIANDA_CMD_STOP,
    'p':            LIANDA_CMD_PROG,
    'prog':         LIANDA_CMD_PROG,
    'us':           LIANDA_CMD_UP_STOP,
    'up+stop':      LIANDA_CMD_UP_STOP,
    'ds':           LIANDA_CMD_DOWN_STOP,
    'down+stop':    LIANDA_CMD_DOWN_STOP,
    'ud':           LIANDA_CMD_UP_DOWN,
    'up+down':      LIANDA_CMD_UP_DOWN,
    'uds':          LIANDA_CMD_UP_DOWN_STOP,
    'up+down+stop': LIANDA_CMD_UP_DOWN_STOP
}

REMOTE_DATABASE = "lianda"

SQL_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS remotes (
        remoteid            INTEGER PRIMARY KEY,
        next_rolling_code   INTEGER,
        state               TEXT DEFAULT \"""" + STATE_UNKNOWN + """\",
        position            INTEGER
    );
"""

SQL_GET_NEXT_CODE = """
    SELECT next_rolling_code
    FROM remotes
    WHERE remoteid = :remoteid
"""

SQL_INSERT_REMOTE = """
    INSERT INTO remotes (remoteid, next_rolling_code)
    VALUES (:remoteid, :next_rolling_code)
"""

SQL_UPDATE_REMOTE = """
    UPDATE remotes
    SET next_rolling_code = :next_rolling_code
    WHERE remoteid = :remoteid
"""

SQL_GET_POSITION = """
    SELECT state, position
    FROM remotes
    WHERE remoteid = :remoteid
"""

SQL_SET_POSITION = """
    UPDATE remotes
    SET position = :position, state = :state
    WHERE remoteid = :remoteid
"""

# SQL_UPSERT_NEXT_CODE = """
#    INSERT INTO remotes (remoteid, next_rolling_code)
#    VALUES (:remoteid, :next_rolling_code)
#    ON CONFLICT(remoteid) DO UPDATE
#        SET next_rolling_code = excluded.next_rolling_code;
# """

# Not supported by current SQLite version
# SQL_UPSERT_NEXT_CODE = """
#    INSERT INTO remotes (remoteid, next_rolling_code)
#    VALUES ({0}, {1})
#    ON CONFLICT(remoteid) DO UPDATE SET next_rolling_code = {1};
# """

myfloat = vol.All(vol.Coerce(float))
#myInt =   vol.All(vol.Coerce(int))
myInt =   vol.All(vol.Coerce(int), vol.Range(min=-100, max=100))

COVER_SCHEMA = vol.Schema({
    vol.Required(CONF_REMOTE_ID): cv.positive_int,
    vol.Optional(CONF_FRIENDLY_NAME): cv.string,
    vol.Optional(CONF_START_ROLLING_CODE,
                 default=DFLT_START_ROLLING_CODE): cv.positive_int,
    vol.Optional(CONF_FULL_OPEN_TIME,
                 default=DFLT_FULL_OPEN_TIME): myfloat,
    vol.Optional(CONF_FULL_CLOSE_TIME,
                 default=DFLT_FULL_CLOSE_TIME): myfloat,
})

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_COVERS, default={}):
    vol.Schema({cv.slug: COVER_SCHEMA}),
    vol.Required(CONF_HOST): cv.string,
    vol.Required(CONF_MAC): cv.string,
    vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
    vol.Optional(CONF_COMMAND_REPEATS, default=DFLT_COMMAND_REPEATS): cv.positive_int,
    vol.Optional(CONF_BROADLINK_SEND_RETRIES, default=DFLT_BROADLINK_SEND_RETRIES): cv.positive_int,
})

SERVICE_RESET_RCODE_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional(CONF_START_ROLLING_CODE): cv.positive_int,
})

SERVICE_SEND_CMND_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    # vol.Required(ATTR_COMMAND): cv.positive_int,
    vol.Required(ATTR_COMMAND): cv.string,
    vol.Optional(ATTR_COMMAND_REPEATS, default=DFLT_COMMAND_REPEATS): cv.positive_int,
})

SERVICE_REMOTE_ADD_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
})

SERVICE_SET_POS_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Required(ATTR_POSITION): cv.positive_int,
})

SERVICE_SET_POS_REL_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Required(ATTR_POSITION): myInt,
})

#COVER_SET_COVER_POSITION_REL_SCHEMA = COVER_SERVICE_SCHEMA.extend({
#    vol.Required(ATTR_POSITION):
#        vol.All(vol.Coerce(int), vol.Range(min=-100, max=100)),
#})


SERVICE_SYNC_POS_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional(ATTR_SYNC_FULLY_OPEN, default=True): cv.boolean,
})


SYMBOL = 640
bitstate = False


def openRemotesDatabase(cover):
    """Open Remotes database"""

    config_dir = cover.hass.config.config_dir

    lianda_db = '{}/{}.db'.format(config_dir, REMOTE_DATABASE)
    _LOGGER.debug(
        "lianda_db: {}"   
        .format(lianda_db
        )
    )



    conn = sqlite3.connect(lianda_db)

    try:
        c = conn.cursor()
        c.execute(SQL_CREATE_TABLE)
        c.close()
        conn.commit()

    except Exception as e:
        # Roll back any change if something goes wrong
        conn.rollback()
        raise e

    finally:
        return conn


def getNextRollingCode(lianda_cover, update=False, increment=0, newcode=None):
    """ Gets the next rolling code for a given remote stored in the
        database. If there is no stored value uses dflt. If update is True
        the new value is stored in the database
    """
    aRemote = lianda_cover._remote_id
    dflt = lianda_cover._start_rolling_code

    conn = openRemotesDatabase(lianda_cover)

    if increment is None:
        increment = 0

    try:
        #        c = conn.cursor()
        #        c.execute(SQL_CREATE_TABLE)
        #        c.close()
        #        _LOGGER.debug("Remote database {} created/opened".format(lianda_db))

        c = conn.cursor()
        c.execute(SQL_GET_NEXT_CODE, {'remoteid': aRemote})
        row = c.fetchone()

        if newcode is not None:
            result = newcode
        else:
            if row is None:
                result = dflt
            else:
                result = int(row[0])
        c.close()

#        _LOGGER.debug(
#            "Stored next Rolling Code for remote {}: {}"
#            .format(hex(aRemote), result))

        if update:
            next_code = result + increment
            c = conn.cursor()

            if row is not None:
                c.execute(SQL_UPDATE_REMOTE,
                          {'remoteid': aRemote, 'next_rolling_code': next_code})
            else:
                c.execute(SQL_INSERT_REMOTE,
                          {'remoteid': aRemote, 'next_rolling_code': next_code})
            c.close()

        conn.commit()
        return result

    # Catch the exception
    except Exception as e:
        # Roll back any change if something goes wrong
        conn.rollback()
        raise e

    finally:
        # Close the db connection
        conn.close()


def saveCoverState(cover):
    """ Saves cover's state"""

    conn = openRemotesDatabase(cover)
    try:
        c = conn.cursor()
        c.execute(SQL_SET_POSITION, {
            'remoteid': cover._remote_id,
            'state': cover._state,
            'position': cover._position})
        c.close()
        conn.commit()

    except Exception as e:
        conn.rollback()
        raise e

    finally:
        # Close the db connection
        conn.close()


def restoreCoverState(cover):
    """ Retrieves cover's saved state"""

    conn = openRemotesDatabase(cover)
    try:
        c = conn.cursor()
        c.execute(SQL_GET_POSITION, {'remoteid': cover._remote_id})
        row = c.fetchone()

        if row is None:
            cover._state = ASSUMED_START_STATE
            cover._position = ASSUMED_START_POSITION
        else:
            _LOGGER.debug("{}:retrieved state/position: {}/{}".
                          format(cover.entity_id, row[0], row[1]))
            cover._state = row[0]
            cover._position = int(row[1]) if not row[1] is None else ASSUMED_START_POSITION

        c.close()

    except Exception as e:
        raise e

    finally:
        # Close the db connection
        conn.close()


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up Broadlink covers."""
    import broadlink

    devices = config.get(CONF_COVERS)
    ip_addr = config.get(CONF_HOST)
    mac_addr = binascii.unhexlify(
        config.get(CONF_MAC).encode().replace(b':', b''))

# 0x2712	RM2	RM
# 0x2737	RM Mini / RM3 Mini Blackbean	RM
# 0x273d	RM Pro Phicomm	RM
# 0x2783	RM2 Home Plus	RM
# 0x277c	RM2 Home Plus GDT	RM
# 0x272a	RM2 Pro Plus	RM
# 0x2787	RM2 Pro Plus2	RM
# 0x278b	RM2 Pro Plus BL	RM


    broadlink_device = broadlink.rm((ip_addr, 80), mac_addr, 0x272a)
    cmnd_repeats = config.get(CONF_COMMAND_REPEATS)
    broadlink_retries = config.get(CONF_BROADLINK_SEND_RETRIES)

    _LOGGER.debug(
        "mac_addr: {}, ip_addr: {}"   
        .format(mac_addr, ip_addr, broadlink_device ))

    _LOGGER.debug(
        "broadlink_device: host: {}, mac: {}, devtype: {}, name: {}, model: {}, manufacturer: {}"   
        .format(broadlink_device.host, 
                broadlink_device.mac, 
                broadlink_device.devtype, 
                broadlink_device.name, 
                broadlink_device.model, 
                broadlink_device.manufacturer
        )
    )

    covers = []
    for object_id, device_config in devices.items():
        covers.append(
            LiandaBroadlinkCover(
                hass,
                object_id,
                broadlink_device,
                broadlink_retries,
                device_config.get(CONF_REMOTE_ID),
                device_config.get(CONF_FRIENDLY_NAME, object_id),
                device_config.get(CONF_FULL_OPEN_TIME),
                device_config.get(CONF_FULL_CLOSE_TIME),
                device_config.get(CONF_START_ROLLING_CODE),
                cmnd_repeats
            )
        )

    add_devices(covers)

    def _svc_reset_rcode(service):
        entity_id = service.data.get(ATTR_ENTITY_ID)
        new_code = service.data.get(CONF_START_ROLLING_CODE)

        if entity_id:
            target_covers = [cover for cover in covers
                             if cover.entity_id in entity_id]
        else:
            target_covers = covers

        for cover in target_covers:
            cover._reset_rolling_code(new_code)

    def _svc_send_command(service):
        entity_id = service.data.get(ATTR_ENTITY_ID)
        command = service.data.get(ATTR_COMMAND)
        lianda_repeats = service.data.get(ATTR_COMMAND_REPEATS)
        broadlink_repeats = service.data.get(ATTR_PACKET_REPEATS)

        if entity_id:
            target_covers = [cover for cover in covers
                             if cover.entity_id in entity_id]
        else:
            target_covers = covers

        for cover in target_covers:
            cover._send_command(command, lianda_repeats, broadlink_repeats)

    def _svc_add_remote(service):
        entity_id = service.data.get(ATTR_ENTITY_ID)

        if entity_id:
            target_covers = [cover for cover in covers
                             if cover.entity_id in entity_id]
        else:
            target_covers = covers

        for cover in target_covers:
            cover._send_command(LIANDA_CMD_PROG, PROG_COMMAND_REPEATS)

    def _svc_set_position(service):
        entity_id = service.data.get(ATTR_ENTITY_ID)
        pos = service.data.get(ATTR_POSITION)

        _LOGGER.debug(
            "{}:set_position service called to position {}"
            .format(entity_id, pos ))

        if entity_id:
            target_covers = [cover for cover in covers
                             if cover.entity_id in entity_id]
        else:
            target_covers = covers

        for cover in target_covers:
            cover._position = pos
            saveCoverState(cover)

    def _svc_set_position_rel(service):
        entity_id = service.data.get(ATTR_ENTITY_ID)
        pos = service.data.get(ATTR_POSITION)

        _LOGGER.debug(
            "{}:set_position_rel service called to position {}"
            .format(entity_id, pos ))

        if entity_id:
            target_covers = [cover for cover in covers
                             if cover.entity_id in entity_id]
        else:
            target_covers = covers

        for cover in target_covers:
            cover.set_rel_cover_position(service.data)


    def _svc_sync_position(service):
        entity_id = service.data.get(ATTR_ENTITY_ID)
        sync_open = service.data.get(ATTR_SYNC_FULLY_OPEN)

        _LOGGER.debug(
            "{}:sync_position service called to fully {} position"
            .format(entity_id, "opened" if sync_open else "closed" ))

        if entity_id:
            target_covers = [cover for cover in covers
                             if cover.entity_id in entity_id]
        else:
            target_covers = covers

        for cover in target_covers:
            cover._do_sync_position(sync_open)

    hass.services.register(DOMAIN,
                          SERVICE_RESET_RCODE,
                          _svc_reset_rcode,
                          schema=SERVICE_RESET_RCODE_SCHEMA)

    hass.services.register(DOMAIN,
                          SERVICE_SEND_CMND,
                          _svc_send_command,
                          schema=SERVICE_SEND_CMND_SCHEMA)

    hass.services.register(DOMAIN,
                          SERVICE_REMOTE_ADD,
                          _svc_add_remote)#,
                        #   schema=SERVICE_REMOTE_ADD_SCHEMA)

    hass.services.register(DOMAIN,
                          SERVICE_SET_POS,
                          _svc_set_position,
                          schema=SERVICE_SET_POS_SCHEMA)
                           
    hass.services.register(DOMAIN,
                          SERVICE_SET_POS_REL,
                          _svc_set_position_rel,
                          schema=SERVICE_SET_POS_REL_SCHEMA)

    hass.services.register(DOMAIN,
                          SERVICE_SYNC_POS,
                          _svc_sync_position,
                          schema=SERVICE_SYNC_POS_SCHEMA)

    broadlink_device.timeout = config.get(CONF_TIMEOUT)
    try:
        broadlink_device.auth()
    except socket.timeout:
        _LOGGER.error("Failed to connect to device")


class LiandaBroadlinkCover(CoverEntity):
    """Representation of an Lianda-Broadlink cover."""

    def __init__(self, hass, object_id, device, retries, remote_id, friendly_name,
                 full_open, full_close, start_rolling_code, cmnd_repeats):
        """Initialize the cover."""

        _LOGGER.debug("""\
{}:Initializing Lianda cover, device: {}, retries: {}, remote_id: {}, \
full_open: {}, full_close: {}, start_rolling_code: {}, cmnd_repeats: {}" \
""".format(ENTITY_ID_FORMAT.format(object_id), device.name, retries, hex(remote_id),
           full_open, full_close, start_rolling_code, cmnd_repeats))

        self.hass = hass
        self._remote_id = remote_id
        self._name = friendly_name

        self.entity_id = ENTITY_ID_FORMAT.format(object_id)

        self._device = device
        self._broadlink_retries = retries

        self._start_rolling_code = start_rolling_code if start_rolling_code else None
        self._lianda_repeats = cmnd_repeats

#        self._state = ASSUMED_START_STATE
#        self._position = ASSUMED_START_POSITION
        restoreCoverState(self)

        self._calculated_position = self._position
        self._target_position = None

        if full_open is None:
            full_open = full_close if full_close else None

        if full_close is None:
            full_close = full_open if full_open else None

        self._full_open = full_open
        self._full_close = full_close

        if full_open or full_close:
            self._update_interval = (1000 * min(full_open, full_close)
                                     ) // (200 / POSITION_RESOLUTION)
        else:
            self._update_interval = None

        self._unsub_listener_cover = None

        self._timer = None

        self._last_sync_key = None
        self._last_rkey = None

    @property
    def name(self):
        """Return the name of the cover."""
        return self._name

    @property
    def assumed_state(self):
        """Return true if unable to access real state of entity."""
        return True

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def current_cover_position(self):
        """Return the current position of the cover."""
        return self._position

    @property
    def is_closed(self):
        """Return if the cover is closed."""
        if self._state == STATE_UNKNOWN:
            return None
        return self._state == STATE_CLOSED

    @property
    def is_closing(self):
        """Return if the cover is closing."""
        if self._state == STATE_UNKNOWN:
            return None
        return self._state == STATE_CLOSING

    @property
    def is_opening(self):
        """Return if the cover is opening."""
        if self._state == STATE_UNKNOWN:
            return None
        return self._state == STATE_OPENING

    @property
    def supported_features(self):
        support_features = SUPPORT_OPEN | SUPPORT_CLOSE | SUPPORT_STOP
        if (self._full_open != 0) or (self._full_close != 0):
            support_features |= SUPPORT_SET_POSITION
        return support_features

###################

###################

    def _time_changed_cover(self, now):
        """Track time changes."""

        global MAX_POSITION_ERROR
#        _LOGGER.debug("_time_changed_cover")
#        _LOGGER.debug("_time_changed_cover. now:{}".format(now))
#        _LOGGER.debug("_time_changed_cover. cover:{}, state: {}"
#                      .format(self._name, self._state))

        if self._state in [STATE_OPENING, STATE_CLOSING]:

            elapsed = now - self._movement_started
            elapsed_ms = 1000 * elapsed.total_seconds()
            old_position = self._position

            if self._state == STATE_CLOSING:
                if self._position is None:
                    self._position = 100
                    self._calculated_position = 100

                delta_pos = (-100 * elapsed_ms) // (self._full_close * 1000)
            else:
                if self._position is None:
                    self._position = 0
                    self._calculated_position = 0

                delta_pos = (100 * elapsed_ms) // (self._full_open * 1000)

            self._calculated_position = self._starting_position + delta_pos
            self._position = max(min(self._calculated_position, 100), 0)

#            if self._position < 0:
#                self._position = 0
#            elif self._position > 100:
#                self._position = 100

            _LOGGER.debug("""\
{}:_time_changed_cover: state:{}, elapsed:{:.2f} ms, delta_pos:{}, \
pos (target/old/new/calc):{}/{}/{}/{}"""
                          .format(self.entity_id, self._state,
                                  elapsed_ms, delta_pos,
                                  self._target_position,
                                  old_position, self._position,
                                  self._calculated_position))

#            if self._target_position not in [0, 100]:
#                tolerance = POSITION_RESOLUTION//2
#            else:
#                tolerance = 0

            tolerance = -POSITION_RESOLUTION
            if (
                (self._target_position is not None)
                and
                (
                    (
                        (self._state == STATE_CLOSING)  # position decreasing
                        and
                        # (self._position <= self._target_position)
                        (self._position - self._target_position <= tolerance)
                    )
                    or
                    (
                        (self._state == STATE_OPENING)  # position increasing
                        and
                        # (self._position >= self._target_position)
                        (self._position - self._target_position >= -tolerance)
                    )
                )
            ):
                _LOGGER.debug(
                    "{}:Stoping cover. Position ({}) over passing target ({})"
                    .format(self.entity_id, self._position, self._target_position))
                self.stop_cover()

            elif abs(self._calculated_position - self._position) > MAX_POSITION_ERROR:
                MAX_POSITION_ERROR = 5 * POSITION_RESOLUTION
                _LOGGER.debug(
                    "{}:Stoping cover. Position ({}) beyond limits"
                    .format(self.entity_id, self._position))
                self.stop_cover()

        self.schedule_update_ha_state()

    def _stop_listener(self):
        """Stops the timer."""
        if self._unsub_listener_cover is not None:
            self._unsub_listener_cover()
            self._unsub_listener_cover = None

        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _listen_cover(self, command, lenghtms):
        """Listen for changes in cover."""

#        _LOGGER.debug("Listening status changes for command: {} every {} secs"
#                    .format(command, time_5_perc))
        self._stop_listener()

        if self._update_interval is not None:
            interval = timedelta(milliseconds=self._update_interval)

            _LOGGER.debug(
                "{}:Starting position check every {} msecs due to command:{}"
                .format(self.entity_id, interval.total_seconds()*1000, command))

            self._movement_started = dt_util.utcnow()
            self._starting_position = self._position
            self._unsub_listener_cover = track_time_interval(
                self.hass, self._time_changed_cover, interval)
#                timedelta(milliseconds=self._update_interval))

            if self._target_position is not None:
                def _timer_stop():
                    """Use timer callback for delayed value refresh."""
                    _LOGGER.debug(
                        "{}:Stoping cover due to Timer timeout"
                        .format(self.entity_id))
                    self._position = self._target_position
                    self.stop_cover()

                if self._timer is not None and self._timer.isAlive():
                    self._timer.cancel()

                deltapos = abs(self._position - self._target_position)
                if self._state == STATE_OPENING:
                    deltatime = deltapos * self._full_open / 100.0
                else:
                    deltatime = deltapos * self._full_close / 100.0

                delay = deltatime - lenghtms / 1000.0
                if delay > 0.1:
                    _LOGGER.debug(
                        "{}:Launching {} timer for {:.2f} secs ({:.2f} - {:.2f} secs)"
                        .format(self.entity_id,
                                "opening" if self._state == STATE_OPENING else "closing",
                                delay, deltatime, lenghtms / 1000.0))
                    self._timer = Timer(delay, _timer_stop)
                    self._timer.start()
                else:
                    _LOGGER.debug(
                        "{}:Delta position too small. Stopping inmediatelly"
                        .format(self.entity_id))
                    _timer_stop()


    def _repetitive_key(self, akey):
        now = dt_util.utcnow()
        last = self._last_sync_key
        self._last_sync_key = now

        oldkey = self._last_rkey
        self._last_rkey = akey

        if last is None:
            self._sync_repeats = 1
            return False

        elapsed = now - last
        elapsed_ms = elapsed.total_seconds() * 1000.0
        if (akey != oldkey) or \
           (elapsed_ms > SYNC_REPEATS_DELAY):
            self._sync_repeats = 1
        else:
            self._sync_repeats += 1
            if self._sync_repeats >= SYNC_REPEATS_KEYS:
                self._sync_repeats = 1
                _LOGGER.debug("{}:_repetitive_key: true".format(self.entity_id))
                return True

        _LOGGER.debug(
            "{}:_repetitive_key: false, repeats: {}, elapsed: {}"
            .format(self.entity_id, self._sync_repeats, elapsed_ms))

        return False

    def _do_open_cover(self, **kwargs):
        """Performs the Opening"""
        _LOGGER.debug("{}:_do_open_cover()".format(self.entity_id))
        self._state = STATE_OPENING
        self._listen_cover('open', self._send_command(LIANDA_CMD_UP))
        self.schedule_update_ha_state()

    def open_cover(self, **kwargs):
        """Open the cover."""
        if ((self._state not in [STATE_OPEN, STATE_OPENING])
                and (self._position is None or self._position < 100) ):
            self._target_position = None
            self._do_open_cover()
        elif self._repetitive_key(STATE_OPENING):
            self._do_sync_position(True)


#    def async_open_cover(self, **kwargs):
#        """Open the cover.
#
#        This method must be run in the event loop and returns a coroutine.
#        """
#        return self.hass.async_add_job(ft.partial(self.open_cover, **kwargs))
#
    def _do_close_cover(self, **kwargs):
        """Performs the Closing"""
        _LOGGER.debug("{}:_do_close_cover()".format(self.entity_id))
        self._state = STATE_CLOSING
        self._listen_cover('close', self._send_command(LIANDA_CMD_DOWN))
        self.schedule_update_ha_state()

    def close_cover(self, **kwargs):
        """Close the cover."""
        if ((self._state not in [STATE_CLOSED, STATE_CLOSING])
                and (self._position is None or self._position > 0) ):
            self._target_position = None
            self._do_close_cover()
        elif self._repetitive_key(STATE_CLOSING):
            self._do_sync_position(False)


#    def async_close_cover(self, **kwargs):
#        """Close cover.
#
#        This method must be run in the event loop and returns a coroutine.
#        """
#        return self.hass.async_add_job(ft.partial(self.close_cover, **kwargs))
#

    def stop_cover(self, **kwargs):
        """Stop the cover."""
        _LOGGER.debug("{}:stop_cover( )".format(self.entity_id))
        if self._state in [STATE_OPENING, STATE_CLOSING]:
            if self._position:
                if self._position == 0:
                    self._state = STATE_CLOSED
                elif self._position == 100:
                    self._state = STATE_OPEN
                else:
                    self._state = STATE_STOPPED
            else:
                self._state = STATE_STOPPED

            self._send_command(LIANDA_CMD_STOP)
            self._stop_listener()

            self._calculated_position = self._position
            self._target_position = None
            saveCoverState(self)
            self.schedule_update_ha_state()
        else:
            _LOGGER.debug("{}:stop_cover( ) => Ignored".format(self.entity_id))


#    def async_stop_cover(self, **kwargs):
#        """Stop the cover.
#
#        This method must be run in the event loop and returns a coroutine.
#        """
#        return self.hass.async_add_job(ft.partial(self.stop_cover, **kwargs))
#
#    def set_cover_position(hass, position, entity_id=None):
#        """Move to specific position all or specified cover."""
#        data = {ATTR_ENTITY_ID: entity_id} if entity_id else {}
#        data[ATTR_POSITION] = position
#        hass.services.call(DOMAIN, SERVICE_SET_COVER_POSITION, data)
#
#    def set_cover_position(self, **kwargs):
#        """Move the cover to a specific position."""
#        pass
#
#    def async_set_cover_position(self, **kwargs):
#        """Move the cover to a specific position.
#
#        This method must be run in the event loop and returns a coroutine.
#        """
#        return self.hass.async_add_job(
#            ft.partial(self.set_cover_position, **kwargs))
#

    def set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        position = kwargs.get(ATTR_POSITION)

        if (self._state not in [STATE_OPENING, STATE_CLOSING]):
            _LOGGER.debug("{}:set_cover_position( {} )".format(self.entity_id, position))

            if abs(self._position - position) <= POSITION_RESOLUTION:
                _LOGGER.debug("{}:set_cover_position => Position change too small. Ignoring".format(self.entity_id))
                return

            if((position >= 0) and (position <= 100)):
                self._target_position = position
                if position > self._position:
                    self._do_open_cover()
                else:
                    self._do_close_cover()
            else:
                _LOGGER.debug("{}:set_cover_position => Invalid position. Ignoring".format(self.entity_id))
        else:
            _LOGGER.debug("{}:set_cover_position => Already moving. Ignoring".format(self.entity_id))

    def set_rel_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        rposition = kwargs.get(ATTR_POSITION)
        position = self._position + rposition

        if (self._state not in [STATE_OPENING, STATE_CLOSING]):
            _LOGGER.debug("{}:set_rel_cover_position( {} )".format(self.entity_id, rposition))

            if abs(self._position - position) <= POSITION_RESOLUTION:
                _LOGGER.debug("{}:set_rel_cover_position => Position change too small. Ignoring".format(self.entity_id))
                return

            if((position >= 0) and (position <= 100)):
                self._target_position = position
                if position > self._position:
                    self._do_open_cover()
                else:
                    self._do_close_cover()
            else:
                _LOGGER.debug("{}:set_rel_cover_position => Invalid position. Ignoring".format(self.entity_id))
        else:
            _LOGGER.debug("{}:set_rel_cover_position => Already moving. Ignoring".format(self.entity_id))

    def update(self):
        """Get updated status from API."""
        if self._state not in [STATE_CLOSING, STATE_OPENING]:
            if self._unsub_listener_cover is not None:
                self._unsub_listener_cover()
                self._unsub_listener_cover = None

    def _do_sync_position(self, sync_open):
        """Syncs logical and physical position at fully open (or closed)"""
        _LOGGER.debug("{}:_do_sync_position( {} )".format(self.entity_id, sync_open))

        if self._state in [STATE_OPENING, STATE_CLOSING]:
            _LOGGER.debug("{}:_do_sync_position ignored because cover is moving".format(self.entity_id))
            return

        if sync_open:
            self._calculated_position = self._position = 100
            self._state = STATE_OPEN
            saveCoverState(self)
            self._send_command(LIANDA_CMD_UP)
        else:
            self._calculated_position = self._position = 0
            self._state = STATE_CLOSED
            saveCoverState(self)
            self._send_command(LIANDA_CMD_DOWN)

    def _send_command(self, cmnd, lianda_repeats=None, broadlink_repeats=None):
        """Build Lianda packet and sends it. Returns packet lenght in mSecs"""
#        _LOGGER.debug("_send_command")

        if cmnd is None:
            _LOGGER.debug("{}:Empty command".format(self.entity_id))
            return 0

        if isinstance(cmnd, str):
            cmnd = BUTTON_MAP[cmnd.lower()]
            
        pktlength = 0

        if lianda_repeats is None:
            lianda_repeats = self._lianda_repeats

        if broadlink_repeats is None:
            broadlink_repeats = DFLT_PACKET_REPEATS

        rcode = getNextRollingCode(self, True, 1)

        LiandaFrame = BuildLiandaFrame(self._remote_id, 0, cmnd, rcode)

#        _LOGGER.debug("Lianda packet [{}]"
#              .format(', '
#                      .join(hex(x).format(':#02X') for x in LiandaFrame)))

        _LOGGER.debug(
            "{}:Lianda packet: remote_id: {}, command: {}, rolling_code: {}"
            .format(self.entity_id, hex(self._remote_id), cmnd, rcode))

        if lianda_repeats > MAX_LIANDA_REPEATS:
            _LOGGER.debug("Too many repeats to fit in one packet. Splitting...")

            _LOGGER.debug("Large packet... Sending first one as single packet")
            ManchesterBits = Manchester_Encode(LiandaFrame, True, 10000)
            BroadlinkFrame = Broadlink_Encode(ManchesterBits, 0)
            pktlength += sum(ManchesterBits)//1000
            self._sendpacket(bytes(BroadlinkFrame))

            _LOGGER.debug(
                "Large packet... Sending remaining {} repeats as Broadlink repeats"
                .format(lianda_repeats - 1))
            ManchesterBits = Manchester_Encode(LiandaFrame, False)
            BroadlinkFrame = Broadlink_Encode(ManchesterBits, lianda_repeats - 1)
            pktlength += (lianda_repeats - 1) * sum(ManchesterBits)//1000
            self._sendpacket(bytes(BroadlinkFrame))
        else:
            ManchesterBits = Manchester_Encode(LiandaFrame, True)

            for i in range(lianda_repeats):
                ManchesterBits += Manchester_Encode(LiandaFrame, False)

            pktlength += sum(ManchesterBits)//1000

            BroadlinkFrame = Broadlink_Encode(ManchesterBits, broadlink_repeats)
            self._sendpacket(bytes(BroadlinkFrame))

        _LOGGER.debug(
            "{}:Command repeated {} times took {} mSecs to transmit"
            .format(self.entity_id, lianda_repeats + 1, pktlength))
        return pktlength

    def _sendpacket(self, packet, retry=None):
        """Send packet to device."""
        # _LOGGER.debug("_sendpacket, len:{}".format(len(packet)))

        if packet is None:
            _LOGGER.debug("Empty packet")
            return True

        if retry is None:
            retry = self._broadlink_retries

        try:
            self._device.send_data(packet)
        except (socket.timeout, ValueError) as error:
            if retry < 1:
                _LOGGER.error(error)
                return False
            if not self._auth():
                return False
            return self._sendpacket(packet, retry - 1)
        return True

    def _reset_rolling_code(self, new_code):
        _LOGGER.debug("{}:_reset_rolling_code called. remoteid:{}, new_rolling_code:{}"
                      .format(self.entity_id, hex(self._remote_id), new_code))

        if new_code is None:
            new_code = self._start_rolling_code

        getNextRollingCode(self, True, 0, new_code)
        return

    def _auth(self, retry=2):
        try:
            auth = self._device.auth()
        except socket.timeout:
            auth = False
        if not auth and retry > 0:
            return self._auth(retry-1)
        return auth

#
# Code to build a lianda protocol frame, Manchester encode and
# insert it on a Broadlink packet ready to be transmitted
#


def BuildLiandaFrame(remote, channel, button, rcode):
    """Builds the data frame"""

    remote += (channel & 0x0F)              # Add channel to remote address

    result = []
    result.append(0x90 | (button & 0x0F))    # result id + Button
    result.append(0xF0)                      # The 4 LSB will be the checksum
    result.append((rcode >> 8) & 0xFF)       # Rolling code(big endian)
    result.append(rcode & 0xFF)
    result.append(remote & 0xFF)             # Remote address (little endian)
    result.append((remote >> 8) & 0xFF)
    result.append((remote >> 16) & 0xFF)

    # Checksum calculation: a XOR of all the nibbles
    checksum = 0
    for i in range(7):
        checksum = checksum ^ result[i] ^ (result[i] >> 4)

    checksum &= 0x0F  # // We keep the last 4 bits only

    # Checksum integration
    result[1] |= checksum   # If a XOR of all the nibbles is equal to 0, the
    # blinds will consider the checksum ok.

    # Obfuscation: a XOR of all the bytes
    for i in range(6):
        result[i + 1] ^= result[i]

    return result


def EncodeBit(bits, bit, lenght):
    """Sends a single state change"""

    global bitstate

    if lenght > 0:
        if (bit == bitstate) & (len(bits) > 0):
            bits[-1] += lenght
        else:
            bits.append(lenght)
    bitstate = bit


def Manchester_Encode(frame, first, last_silence=30415):
    """ Send the command """

#    global bitstate

    result = []
    EncodeBit(result, False, 0)

    if first:           # Only with the first frame.
        # Wake-up pulse & Silence
        EncodeBit(result, True, 9415)
        EncodeBit(result, False, 89565)
        sync = 2
    else:
        sync = 7

    # Hardware sync: two sync for the first frame, seven for the following ones
    for i in range(sync):
        EncodeBit(result, True, 4*SYMBOL)
        EncodeBit(result, False, 4*SYMBOL)

    # Software sync
    EncodeBit(result, True, 4550)
    EncodeBit(result, False, SYMBOL)

    # Data: bits are sent one by one, starting with the MSB.
    for i in range(56):
        if ((frame[i//8] >> (7 - (i % 8))) & 1) == 1 :
            EncodeBit(result, False, SYMBOL)
            EncodeBit(result, True, SYMBOL)
        else:
            EncodeBit(result, True, SYMBOL)
            EncodeBit(result, False, SYMBOL)

    EncodeBit(result, False, last_silence)       # Inter-frame silence
    return result


def Broadlink_Encode(bits, repeat):
    """Builds a Broadlink command frame"""

    # as described in
    # https://github.com/mjg59/python-broadlink/blob/master/protocol.md
    result = []
# The first 4 bytes of the acket are added by the
# broadlink rm::send_ data method
#    result.append(0x02)
#    result.append(0)
#    result.append(0)
#    result.append(0)
    result.append(0xB2)     # RF 433 Mhz
    result.append(repeat)
    result.append(0)        # Will contain data lenght little endian
    result.append(0)

    datalen = 0
    for abit in bits:
        bunits = (abit * 269) // 8192
        if bunits > 255:
            result.append(0)
            result.append((bunits >> 8) & 0xFF)
            bunits &= 0xFF
            datalen += 2
        result.append(bunits)
        datalen += 1

    result[2] = datalen & 0xFF
    result[3] = (datalen >> 8) & 0xFF

    return result
