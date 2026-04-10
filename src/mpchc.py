"""
MPC-HC HTTP client for UC Remote integration.

Parses /variables.html for playback state and sends commands via /command.html.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp

_LOG = logging.getLogger(__name__)

STATE_STOPPED = 0
STATE_PAUSED = 1
STATE_PLAYING = 2

# MPC-HC wm_command IDs (verified against controls.html)
CMD_PLAY_PAUSE = 889
CMD_STOP = 890
CMD_FRAME_STEP = 891
CMD_FRAME_STEP_BACK = 892
CMD_SPEED_DOWN = 894
CMD_SPEED_UP = 895
CMD_SPEED_RESET = 896
CMD_SEEK_BWD_SMALL = 899
CMD_SEEK_FWD_SMALL = 900
CMD_SEEK_BWD_LARGE = 903
CMD_SEEK_FWD_LARGE = 904
CMD_PREV = 921
CMD_NEXT = 922
CMD_VOL_UP = 907
CMD_VOL_DOWN = 908
CMD_MUTE = 909
CMD_FULLSCREEN = 830
CMD_ZOOM_FIT = 836
CMD_ZOOM_100 = 834
CMD_AUDIO_NEXT = 952
CMD_AUDIO_PREV = 953
CMD_SUB_NEXT = 954
CMD_SUB_PREV = 955
CMD_SUB_DELAY_MINUS = 957
CMD_SUB_DELAY_PLUS = 958
CMD_AUDIO_DELAY_MINUS = 945
CMD_AUDIO_DELAY_PLUS = 946
CMD_CHAPTER_NEXT = 918
CMD_CHAPTER_PREV = 916

# Named commands exposed as UC Remote simple commands (prefixed with "mpchc_").
# When routing through the bridge, the "mpchc_" prefix is stripped and the remainder
# is used as the bridge command name (POST /command/<name>).
# The integer values are only used in direct mode (no bridge configured).
MPCHC_COMMANDS: dict[str, int] = {
    # Playback
    "mpchc_play_pause": CMD_PLAY_PAUSE,
    "mpchc_stop": CMD_STOP,
    "mpchc_prev": CMD_PREV,
    "mpchc_next": CMD_NEXT,
    "mpchc_frame_step": CMD_FRAME_STEP,
    "mpchc_frame_step_back": CMD_FRAME_STEP_BACK,
    # Seek
    "mpchc_seek_fwd_small": CMD_SEEK_FWD_SMALL,
    "mpchc_seek_bwd_small": CMD_SEEK_BWD_SMALL,
    "mpchc_seek_fwd_large": CMD_SEEK_FWD_LARGE,
    "mpchc_seek_bwd_large": CMD_SEEK_BWD_LARGE,
    # Chapters
    "mpchc_chapter_next": CMD_CHAPTER_NEXT,
    "mpchc_chapter_prev": CMD_CHAPTER_PREV,
    # Speed
    "mpchc_speed_up": CMD_SPEED_UP,
    "mpchc_speed_down": CMD_SPEED_DOWN,
    "mpchc_speed_reset": CMD_SPEED_RESET,
    # View
    "mpchc_fullscreen": CMD_FULLSCREEN,
    "mpchc_zoom_fit": CMD_ZOOM_FIT,
    "mpchc_zoom_100": CMD_ZOOM_100,
    # Volume
    "mpchc_vol_up": CMD_VOL_UP,
    "mpchc_vol_down": CMD_VOL_DOWN,
    "mpchc_mute": CMD_MUTE,
    # Audio tracks
    "mpchc_audio_next": CMD_AUDIO_NEXT,
    "mpchc_audio_prev": CMD_AUDIO_PREV,
    "mpchc_audio_delay_plus": CMD_AUDIO_DELAY_PLUS,
    "mpchc_audio_delay_minus": CMD_AUDIO_DELAY_MINUS,
    # Subtitles
    "mpchc_sub_next": CMD_SUB_NEXT,
    "mpchc_sub_prev": CMD_SUB_PREV,
    "mpchc_sub_delay_plus": CMD_SUB_DELAY_PLUS,
    "mpchc_sub_delay_minus": CMD_SUB_DELAY_MINUS,
}

_TIMEOUT = aiohttp.ClientTimeout(total=3)


@dataclass
class MpcHcVariables:
    """Parsed state from MPC-HC /variables.html."""

    state: int = STATE_STOPPED
    position: int = 0  # milliseconds
    duration: int = 0  # milliseconds
    volumelevel: int = 0  # 0-100
    muted: int = 0  # 0 or 1
    file: str = ""
    filepath: str = ""
    audio_track: str = ""
    subtitle_track: str = ""


class MpcHcClient:
    """Async HTTP client for MPC-HC web interface.

    When bridge_port is provided, named commands are routed through the bridge's
    POST /command/{name} endpoint (which uses PostMessageW — lower latency than
    MPC-HC's own HTTP interface). State polling always goes directly to MPC-HC.
    """

    def __init__(self, host: str, port: int = 13579, bridge_port: int = 0):
        """Create MPC-HC client. bridge_port > 0 routes commands through the bridge."""
        self._base = f"http://{host}:{port}"
        self._bridge = f"http://{host}:{bridge_port}" if bridge_port > 0 else None
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def get_variables(self) -> MpcHcVariables | None:
        """Fetch and parse /variables.html. Returns None if MPC-HC is not reachable."""
        try:
            async with self._get_session().get(f"{self._base}/variables.html") as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
            return _parse_variables(html)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.debug("MPC-HC not reachable at %s: %s", self._base, ex)
            return None

    async def send_named_command(self, name: str) -> bool:
        """Send a named command via the bridge (POST /command/{name}) or direct MPC-HC HTTP.

        The bridge uses unprefixed names (e.g. "play_pause").  UC Remote simple commands
        carry the "mpchc_" prefix (e.g. "mpchc_play_pause") — the prefix is stripped here.
        Direct calls from device methods already pass the unprefixed bridge name.
        """
        bridge_name = name.removeprefix("mpchc_")
        if self._bridge:
            try:
                async with self._get_session().post(f"{self._bridge}/command/{bridge_name}") as resp:
                    return resp.status == 200
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.debug("MPC-HC bridge command '%s' failed: %s", bridge_name, ex)
                return False
        cmd_id = MPCHC_COMMANDS.get(name)
        return await self.send_command(cmd_id) if cmd_id is not None else False

    async def get_tracks(self) -> dict | None:
        """Fetch audio + subtitle track list from bridge /tracks. Returns None if unavailable."""
        if not self._bridge:
            return None
        try:
            async with self._get_session().get(f"{self._bridge}/tracks") as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.debug("MPC-HC bridge tracks failed: %s", ex)
        return None

    async def select_audio(self, pos: int) -> bool:
        """Select audio track by 0-based position (cycles via bridge)."""
        if self._bridge:
            try:
                async with self._get_session().post(f"{self._bridge}/audio/select/{pos}") as resp:
                    return resp.status == 200
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.debug("MPC-HC bridge audio select failed: %s", ex)
                return False
        return False  # no direct API for track selection without bridge

    async def select_subtitle(self, pos: int) -> bool:
        """Select subtitle track by 0-based position (cycles via bridge)."""
        if self._bridge:
            try:
                async with self._get_session().post(f"{self._bridge}/subtitle/select/{pos}") as resp:
                    return resp.status == 200
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.debug("MPC-HC bridge subtitle select failed: %s", ex)
                return False
        return False

    async def skip(self, offset_ms: int) -> bool:
        """Seek relative to current position by offset_ms milliseconds."""
        if self._bridge:
            try:
                async with self._get_session().post(f"{self._bridge}/skip", params={"offset_ms": offset_ms}) as resp:
                    return resp.status == 200
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.debug("MPC-HC bridge skip failed: %s", ex)
                return False
        # No bridge: read position first, then seek absolute
        vars_ = await self.get_variables()
        if vars_ is None:
            return False
        target_ms = max(0, vars_.position + offset_ms)
        if vars_.duration > 0:
            target_ms = min(target_ms, vars_.duration)
        return await self.seek(target_ms)

    async def seek(self, pos_ms: int) -> bool:
        """Seek to absolute position in milliseconds."""
        if self._bridge:
            try:
                async with self._get_session().post(f"{self._bridge}/seek", params={"pos_ms": pos_ms}) as resp:
                    return resp.status == 200
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.debug("MPC-HC bridge seek failed: %s", ex)
                return False
        try:
            async with self._get_session().get(
                f"{self._base}/command.html",
                params={"wm_command": -1, "position": f"{pos_ms / 1000:.3f}"},
                allow_redirects=False,
            ) as resp:
                return resp.status in (200, 302)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.debug("MPC-HC seek failed: %s", ex)
            return False

    async def set_volume(self, level: int) -> bool:
        """Set volume 0-100."""
        if self._bridge:
            try:
                async with self._get_session().post(f"{self._bridge}/volume", params={"level": level}) as resp:
                    return resp.status == 200
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.debug("MPC-HC bridge volume set failed: %s", ex)
                return False
        try:
            async with self._get_session().get(
                f"{self._base}/command.html",
                params={"wm_command": -2, "volume": level},
                allow_redirects=False,
            ) as resp:
                return resp.status in (200, 302)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.debug("MPC-HC volume set failed: %s", ex)
            return False

    async def send_command(self, wm_command: int) -> bool:
        """Send a raw WM_COMMAND ID directly to MPC-HC's HTTP interface."""
        try:
            async with self._get_session().get(
                f"{self._base}/command.html",
                params={"wm_command": wm_command},
                allow_redirects=False,
            ) as resp:
                return resp.status in (200, 302)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.debug("MPC-HC command %d failed: %s", wm_command, ex)
            return False

    async def close(self):
        """Close the underlying aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None


def _parse_variables(html: str) -> MpcHcVariables:
    """Parse MPC-HC /variables.html and return MpcHcVariables."""
    vars_ = MpcHcVariables()
    for match in re.finditer(r'<p\s+id="([^"]+)">([^<]*)</p>', html):
        key, value = match.group(1), match.group(2).strip()
        try:
            if key == "state":
                vars_.state = int(value)
            elif key == "position":
                vars_.position = int(value)
            elif key == "duration":
                vars_.duration = int(value)
            elif key == "volumelevel":
                vars_.volumelevel = int(value)
            elif key == "muted":
                vars_.muted = int(value)
            elif key == "file":
                vars_.file = value
            elif key == "filepath":
                vars_.filepath = value
            elif key == "audiotrack":
                vars_.audio_track = value
            elif key == "subtitletrack":
                vars_.subtitle_track = value
        except ValueError:
            pass
    return vars_


class MpcHcBridgeWs:
    """Persistent WebSocket client for mpchc-bridge /ws push endpoint.

    Calls the registered async callback with a dict of changed fields on every push.
    Reconnects automatically after any disconnection.
    """

    def __init__(self, host: str, port: int):
        """Create WebSocket client pointing at ws://host:port/ws."""
        self._url = f"ws://{host}:{port}/ws"
        self._callback: Callable[[dict], Awaitable[None]] | None = None

    def set_callback(self, fn: Callable[[dict], Awaitable[None]]) -> None:
        """Register async callback invoked with changed-field dict on every push."""
        self._callback = fn

    async def run(self) -> None:
        """Run the reconnect loop — wrap this in an asyncio.Task."""
        while True:
            try:
                async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                    async with session.ws_connect(self._url, heartbeat=30) as ws:
                        _LOG.debug("MPC-HC bridge WebSocket connected: %s", self._url)
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT and self._callback:
                                try:
                                    await self._callback(json.loads(msg.data))
                                except Exception as ex:  # pylint: disable=broad-exception-caught
                                    _LOG.debug("MPC-HC WS callback error: %s", ex)
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                break
            except asyncio.CancelledError:
                return
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.debug("MPC-HC bridge WS disconnected (%s), retry in 5 s", ex)
            await asyncio.sleep(5)
