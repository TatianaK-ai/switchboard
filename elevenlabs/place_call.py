"""Place a real voice call to the agent, with a synthesised caller.

    python elevenlabs/place_call.py                 # the VPN script

    python elevenlabs/place_call.py locked          # the locked-account script

This is a genuine end-to-end test, not a simulation. The caller's lines are turned into

speech with ElevenLabs TTS and streamed into the Conversational AI websocket as audio,

so the path exercised is the real one: audio -> their ASR -> the agent -> our webhook

-> the LangGraph service -> the reply -> their TTS -> audio back.

The dashboard's `simulate-conversation` endpoint does NOT do this: it stubs every tool

call with the literal string "Tool Called." and never touches the webhook, which is why

an agent that looked fine there said its failure line on every turn.

"""

from __future__ import annotations

import asyncio

import base64

import json

import os

import sys

import urllib.request
import wave

import websockets

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

KEY = os.getenv("ELEVENLABS_API_KEY", "")

AGENT_ID = os.getenv("AGENT_ID", "agent_7701m176hfcbejrsaa1q4kb8k4pr")

# Any stock voice; this is the *caller*, not the agent.

CALLER_VOICE = os.getenv("CALLER_VOICE_ID", "CwhRBWXzGAHq8TQ4Fs17")

SCRIPTS: dict[str, list[str]] = {

    "vpn": [

        "Hi, my VPN keeps dropping every few minutes.",

        "My employee id is E 2 2 7 7.",

        "Zero four two one.",

        "I'm on wifi, two point four gigahertz.",

        "Okay, I switched it to five gigahertz.",

        "Done, power saving is off too.",

        "Yes, it's been solid for five minutes now. That fixed it.",

    ],

    "locked": [

        "I'm locked out of my account.",

        "My employee id is E 4 0 8 8.",

        "One one seven six.",

        "Yes please, go ahead.",

    ],

    "badid": [

        "My VPN doesn't work.",

        "E 2 3 4 5.",

        "E 2 3 4 5.",

        "Sorry, it's E 2 2 7 7.",

        "Zero four two one.",

    ],

}

def _wav(path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm)

def tts(text: str) -> bytes:

    """Caller's line as 16 kHz PCM, which is what the conversation socket wants."""

    req = urllib.request.Request(

        f"https://api.elevenlabs.io/v1/text-to-speech/{CALLER_VOICE}"

        "?output_format=pcm_16000",

        data=json.dumps({"text": text, "model_id": "eleven_turbo_v2_5"}).encode(),

        headers={"xi-api-key": KEY, "Content-Type": "application/json"},

        method="POST")

    with urllib.request.urlopen(req, timeout=60) as r:

        return r.read()

async def place(script: list[str]) -> None:

    url = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={AGENT_ID}"

    async with websockets.connect(url, additional_headers={"xi-api-key": KEY},

                                  max_size=16 * 1024 * 1024) as ws:

        await ws.send(json.dumps({"type": "conversation_initiation_client_data"}))

        line_no = 0

        conv_id = None

        said_something = asyncio.Event()
        # Both sides of the call, kept so a recording can play the real thing
        # rather than a re-enactment.
        agent_pcm = bytearray()
        mixed_pcm = bytearray()

        async def listen():

            nonlocal conv_id

            async for raw in ws:

                m = json.loads(raw)

                t = m.get("type")

                if t == "conversation_initiation_metadata":

                    conv_id = m["conversation_initiation_metadata_event"][

                        "conversation_id"]

                    print(f"  [conversation {conv_id}]\n")

                elif t == "user_transcript":

                    print(f"  caller > "

                          f"{m['user_transcription_event']['user_transcript']}")

                elif t == "audio":
                    chunk = base64.b64decode(
                        m["audio_event"]["audio_base_64"])
                    agent_pcm.extend(chunk)
                    mixed_pcm.extend(chunk)
                elif t == "agent_response":

                    print(f"   agent > "

                          f"{m['agent_response_event']['agent_response']}\n")

                    said_something.set()

                elif t == "ping":

                    await ws.send(json.dumps({"type": "pong",

                                              "event_id": m["ping_event"]["event_id"]}))

                elif t == "interruption":

                    said_something.set()

        task = asyncio.create_task(listen())

        await asyncio.sleep(3)          # let the agent deliver its opening line

        # 20ms frames at 16 kHz mono 16-bit. Silence has to be SENT, not merely waited

        # through: the far end runs voice-activity detection over the stream, and a gap

        # in transmission is not the same as a gap in speech. Without these frames all

        # seven lines arrived as one unbroken utterance and the agent answered once.

        frame = 640

        silence = bytes(frame)

        for text in script:

            audio = tts(text)

            for i in range(0, len(audio), frame):

                await ws.send(json.dumps({

                    "user_audio_chunk": base64.b64encode(

                        audio[i:i + frame]).decode()}))

                await asyncio.sleep(0.02)

            said_something.clear()

            # ~1.6s of silence, which is comfortably past the default end-of-turn

            # threshold without being long enough to read as the caller hanging up.

            for _ in range(80):

                await ws.send(json.dumps({

                    "user_audio_chunk": base64.b64encode(silence).decode()}))

                await asyncio.sleep(0.02)

            try:

                await asyncio.wait_for(said_something.wait(), timeout=25)

            except asyncio.TimeoutError:

                print("   [no reply within 25s]")

            line_no += 1

        await asyncio.sleep(3)

        task.cancel()

        out_dir = pathlib.Path(__file__).resolve().parent.parent / "out"
        out_dir.mkdir(exist_ok=True)
        for name, pcm in (("agent", agent_pcm), ("call", mixed_pcm)):
            if pcm:
                _wav(out_dir / (name + "-audio.wav"), bytes(pcm), 16000)
                print(f"  [wrote out/{name}-audio.wav "
                      f"{len(pcm) / 32000:.1f}s]")
        print(f"\n  [call finished after {line_no} caller turns]")

        if conv_id:

            print(f"  [conversation id {conv_id}]")

def main() -> None:

    if not KEY:

        raise SystemExit("ELEVENLABS_API_KEY is not set")

    name = sys.argv[1] if len(sys.argv) > 1 else "vpn"

    if name not in SCRIPTS:

        raise SystemExit(f"unknown script {name!r}; try: {', '.join(SCRIPTS)}")

    print(f"\n=== placing call: {name} ===\n")

    asyncio.run(place(SCRIPTS[name]))

if __name__ == "__main__":

    main()

