import os
import sys
import types
from enum import Enum

from pydantic import BaseModel


if os.environ.get("FLOWSPEECH_STANDALONE_TESTS") == "1":
    # Protocol tests can run without TEN's native binaries. Normal TEN tests use the real interfaces.
    runtime = types.ModuleType("ten_runtime")
    runtime.AsyncTenEnv = type("AsyncTenEnv", (), {})
    base = types.ModuleType("ten_ai_base")
    base.__path__ = []
    utils = types.ModuleType("ten_ai_base.utils")
    utils.encrypt = lambda value: "<redacted>"
    const = types.ModuleType("ten_ai_base.const")
    const.LOG_CATEGORY_VENDOR = "vendor"
    struct = types.ModuleType("ten_ai_base.struct")
    struct.TTS2HttpResponseEventType = Enum(
        "TTS2HttpResponseEventType",
        ["RESPONSE", "END", "ERROR", "INVALID_KEY_ERROR", "FLUSH"],
    )
    http = types.ModuleType("ten_ai_base.tts2_http")
    http.AsyncTTS2HttpConfig = BaseModel
    http.AsyncTTS2HttpClient = type("AsyncTTS2HttpClient", (), {})

    class Extension:
        def __init__(self, name):
            self.name = name

    http.AsyncTTS2HttpExtension = Extension
    base.utils = utils
    for module in (runtime, base, utils, const, struct, http):
        sys.modules[module.__name__] = module
