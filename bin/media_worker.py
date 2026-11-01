#!/usr/bin/python3 -I
"""Fixed offline media decoder/transcriber; executed only inside the namespace."""
import base64
import json
from pathlib import Path
import subprocess
import sys


def main():
    if sys.argv[1:] == ["probe"]:
        import socket
        assert not Path('/home').exists()
        assert not Path('/run').exists()
        assert not Path('/etc/youtube-obsidian').exists()
        assert not socket.if_nameindex() or len(socket.if_nameindex()) == 1
        print(json.dumps({'isolated': True, 'network': 'private', 'credentials': 'absent'}))
        return
    source = Path('/input/media')
    info = json.loads(subprocess.run(['/usr/bin/ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                     '-show_format', '-show_streams', '-of', 'json', str(source)],
                                    capture_output=True, text=True, check=True, timeout=30).stdout)
    duration = float(info['format']['duration'])
    if not 0 < duration <= 900:
        raise ValueError('Media must be between zero and 900 seconds')
    streams = {s.get('codec_type') for s in info['streams']}
    transcript, segments, frames = '', [], []
    if 'audio' in streams:
        # Decode locally with remote protocols disabled before PyAV/Whisper.
        wav = Path('/output/audio.wav')
        subprocess.run(['/usr/bin/ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                        '-i', str(source), '-vn', '-ar', '16000', '-ac', '1', str(wav)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        sys.path.insert(0, '/runtime')
        from faster_whisper import WhisperModel
        model = WhisperModel('/model', device='cpu', compute_type='int8', cpu_threads=4,
                             num_workers=1, local_files_only=True)
        result, _ = model.transcribe(str(wav), beam_size=5, vad_filter=True,
                                     condition_on_previous_text=False)
        for segment in result:
            segments.append({'start': round(segment.start, 2), 'end': round(segment.end, 2), 'text': segment.text.strip()})
        transcript = '\n'.join(f"[{s['start']:.2f}-{s['end']:.2f}] {s['text']}" for s in segments)
        if len(transcript) > 300_000:
            raise ValueError('Transcript exceeds limit')
    if 'video' in streams:
        for i in range(8):
            stamp = duration * (i + 0.5) / 8
            path = Path(f'/output/frame-{i}.jpg')
            subprocess.run(['/usr/bin/ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                            '-ss', str(stamp), '-i', str(source), '-frames:v', '1',
                            '-vf', 'scale=960:960:force_original_aspect_ratio=decrease', '-q:v', '3', str(path)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if path.exists():
                frames.append({'timestamp': round(stamp, 2), 'base64': base64.b64encode(path.read_bytes()).decode()})
    print(json.dumps({'transcript': transcript, 'duration': duration, 'frames': frames,
                      'audio_present': 'audio' in streams, 'model': 'faster-whisper-large-v3-turbo',
                      'model_revision': '0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf'}))


if __name__ == '__main__':
    main()
