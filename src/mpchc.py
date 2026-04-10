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

# MPC-HC wm_command IDs
CMD_PLAY_PAUSE = 889
CMD_STOP = 890
CMD_PREV = 921
CMD_NEXT = 922
CMD_VOL_UP = 907
CMD_VOL_DOWN = 908
CMD_MUTE = 909
CMD_SEEK_FWD = 899
CMD_SEEK_BWD = 900
CMD_SEEK_FWD_L = 901
CMD_SEEK_BWD_L = 902
CMD_FULLSCREEN = 830
CMD_AUDIO_NEXT = 952
CMD_SUB_PREV = 953
CMD_SUB_NEXT = 954
CMD_SUB_TOGGLE = 955
CMD_SUB_DELAY_MINUS = 957
CMD_SUB_DELAY_PLUS = 958
CMD_AUDIO_DELAY_MINUS = 945
CMD_AUDIO_DELAY_PLUS = 946

# Named commands exposed as UC Remote simple commands (lowercase, prefixed with "mpchc_")
MPCHC_COMMANDS: dict[str, int] = {
    "mpchc_play_pause": CMD_PLAY_PAUSE,
    "mpchc_stop": CMD_STOP,
    "mpchc_fullscreen": CMD_FULLSCREEN,
    "mpchc_audio_next": CMD_AUDIO_NEXT,
    "mpchc_audio_delay_plus": CMD_AUDIO_DELAY_PLUS,
    "mpchc_audio_delay_minus": CMD_AUDIO_DELAY_MINUS,
    "mpchc_sub_next": CMD_SUB_NEXT,
    "mpchc_sub_prev": CMD_SUB_PREV,
    "mpchc_sub_toggle": CMD_SUB_TOGGLE,
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
    """Async HTTP client for MPC-HC web interface."""

    def __init__(self, host: str, port: int = 13579):
        """Create MPC-HC client for the given host and port."""
        self._base = f"http://{host}:{port}"
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

    async def send_command(self, wm_command: int) -> bool:
        """Send a command to MPC-HC. Returns True on success."""
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
