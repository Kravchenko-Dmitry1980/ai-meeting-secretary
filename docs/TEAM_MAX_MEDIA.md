# MAX media boundary (T8)

`MAXMediaClient` downloads one audio/file attachment from a previously verified,
bound `BotEvent`. `VoiceCommandService` must recheck the current event claim,
member revision, and project access immediately before invoking it. The media
adapter does not authenticate a member, recognize speech, spend provider credit,
or alter a task.

## Contract

```python
MAXMediaClient(
    *, allowlisted_hosts=(), client, ffprobe_path, ffmpeg_path, scratch_dir,
    resolver=None, process_runner=None, clock=None,
)
download_voice(event, *, cancelled=None) -> ValidatedAudio
release(audio) -> None
cleanup_expired(*, now=None) -> int
```

`ValidatedAudio` is frozen: `path: Path` (hidden from repr), `duration_ms: int`,
`sha256: str`, `byte_size: int`, and canonical UUID string `command_id` equal to
`event.event_id`. Its bytes are the validated mono, 16 kHz, signed 16-bit PCM WAV.
Duration uses ceiling milliseconds from decoded frame count; metadata supplied
by MAX is not evidence of duration. SHA-256 covers the exact normalized file.
`MaxMediaError` exposes only an allowlisted `.code`; callers must not log the
original attachment, signed URL, local artifact path, or raw process output.

Limits are 10 MiB of actual downloaded bytes and 120 seconds of decoded audio.
Exactly one audio stream is required. Probe metadata exceeding the duration
limit is rejected early; decoding independently verifies the actual limit.
Decode runs up to 121 seconds of audio so an overlong source cannot be accepted
by truncating it at the permitted duration.

## Configuration and transport

There is deliberately no default CDN host. An empty allowlist returns
`max_media_configuration_required` before DNS/HTTP. Configure exact lowercase
HTTPS host names only after separate owner probes establish the actual MAX
audio-file and native-voice contracts. Redirects are rejected, including a
redirect to another allowed host. No bot token, Authorization header, or cookie
is sent to a media host.

Supply a dedicated exact `httpx.Client`, with `trust_env=False`, no auth,
cookies, hooks, query defaults, base URL, proxies, redirects or custom mounts,
and finite timeouts no greater than 10 seconds. The native transport must use
verified TLS, HTTP/1 only, zero retries, and zero keepalive connections. For
example, construct its `HTTPTransport` with `trust_env=False`, `http2=False`,
`retries=0`, and `Limits(max_keepalive_connections=0)`. Do not share this client
or mutate it while the adapter is running. Exact `MockTransport` is a trusted
offline test injection, not production TLS evidence.

All resolver answers must be public IPs; mixed public/private results fail.
The request connects to one validated numeric IP with the original Host and
TLS SNI name. The adapter checks the peer address when available. HTTP/2 and
connection reuse are disabled to prevent pooling a connection for a different
SNI name. Private, loopback, link-local, multicast, site-local, mapped and known
tunnel addresses are rejected. There is no second hostname resolution by the
HTTP transport and no global DNS monkeypatch.

These checks depend on the installed HTTPX/httpcore implementation. The adapter
verifies the native transport/backend types and settings before each download;
dependency upgrades require rerunning the pinned-IP/SNI and configuration
tests. The implementation was checked against the installed sources for
`httpcore._sync.connection` and `httpx._transports.default`.

The total DNS/download deadline is 30 seconds; DNS has a 5-second subdeadline.
Socket operations recompute the remaining deadline to bound slow trickle
responses. A timed-out system DNS call can remain in one daemon thread until
the OS resolver returns; the adapter refuses another lookup while that thread
is alive. The thread never downloads or writes artifacts. HTTPX/httpcore logs
are redacted in this operation's context so signed queries and response data
cannot appear in their normal logging records.

## CPU processing and artifact ownership

FFprobe and FFmpeg paths must be explicitly configured absolute local paths to
existing binaries. UNC/device paths are rejected before filesystem probing.
No installation or PATH discovery occurs. Injected runners are trusted test
dependencies. Each native process has a 30-second deadline, bounded stdout,
discarded stderr, no shell, no device input, CPU decode, one decode thread,
bounded allocation, and explicit demuxer/codec and `file,pipe` protocol lists.
Playlists and additional video/attachment streams fail validation. Child
environment contains only Windows system-directory variables, owned TEMP/TMP,
and a logging-color setting; FFREPORT and provider credentials are not inherited.
These process restrictions are not an operating-system sandbox for native
decoder vulnerabilities; only trusted configured binaries belong here.

The adapter creates random flat `voice-<uuidhex>` directories below its owned
scratch root. The manifest contains command UUID, timestamps, state, and
validated-file metadata, never a source URL or token. Symlinks and Windows
reparse points in the root, ancestors, job, or job contents are rejected.
Cleanup verifies the exact parent and generated name and never recursively
deletes an arbitrary path. Process failures and cooperative cancellation remove
the owned job. A successful download removes compressed source bytes and keeps
only the normalized WAV and manifest.

The application owns the successful artifact's lifecycle. It can call
idempotent `release(audio)` after use; it must keep the file while an STT worker
can still read it. T12 must invoke `cleanup_expired()` explicitly: validated
artifacts expire after 24 hours and abandoned processing jobs after one hour.
No background cleanup or automatic redownload is started by this adapter.

## Evidence and remaining live gates

`tests/test_max_media.py` uses synthetic media/metadata, mocked HTTP/DNS, and
guarded CPU subprocesses. The native CPU fixture verifies actual FFprobe and
FFmpeg normalize generated stereo 44.1 kHz PCM to mono 16 kHz WAV. The suite
covers URL/IP refusal, byte/duration limits, injected codec/probe failures,
deadline/cancellation, environment isolation, signed-URL log redaction,
reparse/UNC rejection, and artifact cleanup/retention.

This is offline boundary evidence. Real MAX CDN hosts, signed-URL behavior,
TLS exchange with MAX, native voice-message payloads, audio-file payloads, and
their codec coverage remain separate owner live gates. Neither the native CPU
fixture nor a mock HTTP response qualifies these external contracts.
