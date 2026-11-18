# Uncle Virgil

A private voice notebook. It records what you say, turns it into text labeled
by speaker and time, reminds you of things you ask it to remember, and writes
an end-of-day summary of what you worked on. Speech processing runs on your
own computer.

This is the first milestone: a Windows desktop app that uses the computer's
microphone. An Android app, syncing between the two, and a wearable
microphone are planned but not built.

## What it does today

- **Dictate**: press a button to record, press again to stop.
- **Continuous recording**: listens all day and keeps only the stretches that
  contain speech.
- **Transcripts** with a timestamp and speaker on every line. Your voice (and
  anyone else you name) is recognized by name; other voices appear as
  "Speaker 1", "Speaker 2". Play any line, correct its words or speaker, or
  delete a conversation that should not have been recorded.
- **Search** across everything you have recorded, under History.
- **Reminders**: say "note to self, meeting with Jack at 5 pm" and you are
  notified at 4:30. Notification by desktop toast, email, or phone push.
  When a reminder is heard, a confirmation shows the time it was understood
  as, so a misheard time can be fixed. Reminders can be changed or snoozed.
- **Daily summary**: work performed, time per task with sub-tasks, and your
  notes to self and ideas. Pages for today, yesterday, and any past day.
- **Storage controls**: delete audio in a date range while keeping the text,
  or compress audio in a date range.
- **Import** recordings made elsewhere (phone, voice recorder); they are
  placed in the day and time they were recorded.
- **Your choice of language model**: none, Claude, ChatGPT, or a model
  running on your own computer.

## Install on Windows

**Standalone (no Python needed):** open the repository's
[Actions tab](https://github.com/twlenz/Uncle-Virgil/actions/workflows/tests.yml),
choose the latest successful run, and download **Uncle-Virgil-windows** under
Artifacts. Unzip it anywhere and run `Uncle Virgil.exe`. The build is not
code-signed, so the first time Windows may show "Windows protected your PC";
choose More info, then Run anyway.

**From source:**

1. Install Python 3.12 or 3.13 from [python.org](https://www.python.org/downloads/windows/).
2. Download this repository and double-click `start.bat`. The first run
   installs the app into a `.venv` folder next to it, then opens the window.

Either way, then follow the **Getting started** list on the Today page:
download the speech models (about 500 MB, one time), then record a
20-second voice sample.

Press **Ctrl+Alt+D** in any program to start dictating, and again to stop;
a short sound confirms each press. The shortcut can be changed or turned off
under Settings > Recording.

To record all day, switch on **Continuous recording**, and under Settings >
Recording tick "Start Uncle Virgil when I sign in to Windows". The app then
opens minimized at sign-in and resumes recording. Closing the window while it
is recording asks first, since closing stops the recording.

If no audio is picked up, check Windows Settings > Privacy & security >
Microphone > "Let desktop apps access your microphone".

The window uses Microsoft Edge WebView2, which ships with Windows 11 and
current Windows 10.

## Privacy

Recording, transcription, speaker recognition, reminder detection and storage
all happen on your computer. The app's internal service listens only on
`127.0.0.1` and requires a token that changes on every launch.

Data leaves the computer only in these cases, each of which you switch on:

| When | What is sent | To |
| --- | --- | --- |
| Downloading speech models | Nothing of yours; a file download | GitHub |
| A hosted language model is selected (Claude, OpenAI) | Transcript text, when a summary is made | That provider |
| "Use the language model to find reminders" is on, with a hosted model | Your own words from each recording | That provider |
| Email notifications | The reminder text | Your SMTP server |
| Phone push (ntfy) | The reminder text | The ntfy server you choose |

Audio is never sent anywhere. API keys and passwords are kept in Windows
Credential Manager, not in the settings file.

Voiceprints of people you name are stored in `speakers.json`. Voiceprints of
unnamed speakers are kept only in that day's folder and are never matched
across days. Recording other people is subject to the law where you are.

## Safety: what it does to your computer

- **Files**: the app writes only inside its own data folder (below) and, for
  the source install, the `.venv` folder next to `start.bat`. It never deletes
  anything automatically. Deleting happens only when you choose it (delete
  audio in a date range, delete a conversation or line, forget a voice), each
  after a confirmation. A recording in which no words were recognized is kept
  in that day's `audio\no-speech` folder.
- **Crashes and power cuts**: every save goes to a temporary file that is
  flushed to disk before it replaces the old one, so a file is never left
  half-written. If a file is damaged anyway, it is kept under a
  `.damaged-<date>` name and the app carries on; it is never thrown away. At
  most the last few minutes of audio not yet saved can be lost.
- **One copy at a time**: a second copy of the app on the same data folder
  refuses to start, so two copies never record or write at once.
- **Disk space**: a warning appears below 5 GB free; recording stops below
  1 GB free and resumes once space is freed, so the drive never fills up.
- **Processor and memory**: transcription runs at below-normal priority on at
  most half the processor cores, so other programs come first. The app uses
  about 1 to 1.5 GB of memory while running (the speech models). On a
  computer with 8 GB or less, choosing the Whisper tiny or base model in
  Settings uses less.
- **Network**: the app's internal service accepts connections only from this
  computer, and only with a key that changes at every start. It contacts the
  internet only for the uses listed under Privacy above.
- **System settings**: the only change outside the data folder is optional:
  "Start with Windows" adds one entry named "Uncle Virgil" to your user's
  startup list (no administrator rights; turning it off removes it). API keys
  you save go to Windows Credential Manager under "uncle-virgil".

To remove the app completely: turn off "Start with Windows" in Settings,
close the app, then delete the app folder and the data folder. Saved keys
can be removed in Windows Credential Manager (entries named
"uncle-virgil").

## Where your data lives

Everything is under `C:\Users\<you>\UncleVirgil` (change it with the
`UNCLE_VIRGIL_HOME` environment variable), in formats other programs can open:

```
days/2026-11-16/
    audio/14-03-22.flac     one file per recording, named by start time
    transcript.md           the day's transcript, for reading
    segments.jsonl          the same content, one JSON object per utterance
    summary.md              the daily summary
    summary.json
reminders.json
speakers.json
config.toml
```

New audio is lossless FLAC, which measured 50 to 80 MB per hour of kept speech
in testing (more with background noise). Compressing converts it to Opus at
10 to 12 MB per hour.

## Language models

Choose one under **Settings > Language model**:

- **None**: reminders and notes still work, through spoken trigger phrases.
  Summaries list notes and reminders but cannot identify tasks.
- **Claude**: needs an Anthropic API key. Default model `claude-opus-5-5`.
- **OpenAI-compatible**: covers OpenAI and local model servers. For a fully
  local setup, install [Ollama](https://ollama.com), run `ollama pull` for a
  model, and enter its name. Without a graphics card, pick a small model;
  a day's summary can take several minutes.

Time per task is computed from timestamps by the app, not by the model. Each
conversation counts for its own length plus up to 20 minutes of the quiet
time after it (configurable; 0 counts speaking time only).

## Extending it

Language-model providers, notification channels, and event handlers can be
added by a separate Python package, without changing this one. Declare an
entry point in the `uncle_virgil.plugins` group that points at a
`setup(app)` function:

```python
import uncle_virgil.llm

def setup(app):
    # a new language-model provider, selectable in Settings
    uncle_virgil.llm.register("my-llm", lambda settings, get_secret: MyProvider())
    # a new way to be notified
    app.notifier.channels["sms"] = lambda title, body: send_sms(f"{title}: {body}")
    # react to what the app does (the hook for future agents)
    app.events.subscribe("day_summarized", lambda date, summary: ...)
```

A provider is any object with `name`, `off_device`, and
`complete(system, prompt, schema=None) -> str`. Events:
`block_transcribed`, `reminder_created`, `reminder_fired`, `day_summarized`.

## How it works

```
microphone -> recorder (speech detection, cut into blocks) -> audio file
           -> speaker separation + transcription + voiceprint matching
           -> segments.jsonl / transcript.md
           -> reminder detection -> scheduler -> notifications
           -> daily summary (language model labels tasks; app computes time)
```

Speech runs on the CPU through [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx):
Silero for speech detection, pyannote segmentation plus TitaNet voiceprints
for speakers, and NVIDIA Parakeet (or Whisper) for transcription. The window
is [pywebview](https://pywebview.flowrl.com) over a local FastAPI service.

| Module | Role |
| --- | --- |
| `recorder.py` | Microphone capture, dictation and continuous modes |
| `speech.py`, `models.py` | Speech engine and model downloads |
| `speakers.py` | Enrolled voices and per-day unnamed speakers |
| `pipeline.py` | Background transcription queue |
| `reminders.py`, `notify.py` | Note detection, time parsing, scheduling, channels |
| `summary.py`, `llm/` | Daily summary and model providers |
| `storage.py` | The on-disk layout, search, corrections, delete and compress |
| `autostart.py`, `hotkey.py` | Start with Windows, and the dictation shortcut |
| `server.py`, `ui/` | Local API and the window's pages |

## Known limits

- English only.
- Transcription mishears words, including the "note to self" trigger. The
  trigger is matched loosely to compensate; switching on the language-model
  reminder pass catches more.
- A speaker heard for under 1.5 seconds in a recording is merged into the
  speaker next to them.
- Text-message notifications are not built. Phone push through ntfy covers
  the same need; SMS can be added as a notification channel plugin.
- The app must be open (it can be minimized) to record. There is no
  system-tray mode yet.
- Importing reads FLAC, WAV, Ogg/Opus and MP3. Phone recordings in M4A/AAC
  need [ffmpeg](https://ffmpeg.org) installed.
- Automated tests run on Windows (GitHub Actions) and cover the registry
  entry for starting with Windows, Credential Manager, the notification
  system, the dictation shortcut, and opening the real window. What they
  cannot cover is a real microphone and a person clicking through the
  window on a real PC.

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

Tests that need the speech models are skipped unless
`UNCLE_VIRGIL_TEST_MODELS` points at a folder containing them (the app's own
`models` folder works).

Run the service without the window, for debugging in a browser:

```bash
uncle-virgil --no-window
```

Check an installation (opens the window, checks it reaches the service and
that the speech engine runs, then exits; the result is also written to
`uncle-virgil.log`):

```bash
uncle-virgil --self-test
```

Download the speech models without opening the window, for example on a
machine you will move offline:

```bash
uncle-virgil --download-models
```

`UNCLE_VIRGIL_MODELS` points several data folders at one shared copy of the
models. Each model file is checked against a pinned SHA-256 before use, and
an interrupted download resumes where it stopped.

Build the standalone Windows app yourself (PowerShell, from the repository
folder):

```
.\packaging\build-windows.ps1
```
