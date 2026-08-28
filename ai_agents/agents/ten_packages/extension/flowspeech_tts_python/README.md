# flowspeech_tts_python

TEN Framework text-to-speech extension for [FlowSpeech](https://flowspeech.io/).
It sends text to the FlowSpeech HTTP API and emits 24 kHz mono 16-bit PCM audio
for voice-agent pipelines.

## Configuration

Create an API key in the FlowSpeech settings page, then set:

```bash
FLOWSPEECH_API_KEY=your_api_key
```

The default `property.json` uses the `Kore` voice. You can select another
FlowSpeech voice by changing `params.voice_name`.

```json
{
  "params": {
    "api_key": "${env:FLOWSPEECH_API_KEY}",
    "base_url": "https://flowspeech.io",
    "voice_name": "Kore"
  }
}
```

Do not commit an API key to source control.

## API

Refer to the TTS interface in `manifest.json` and the defaults in
`property.json`.
