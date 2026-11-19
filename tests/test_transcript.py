from pathlib import Path
from src.transcript import clean_vtt

FIX = Path(__file__).parent / "fixtures" / "sample.vtt"

def test_clean_vtt_removes_header_and_timings():
    out = clean_vtt(FIX.read_text())
    assert "WEBVTT" not in out
    assert "-->" not in out
    assert "align:start" not in out
    assert "<c>" not in out and "<00:" not in out

def test_clean_vtt_dedupes_rolling_lines():
    out = clean_vtt(FIX.read_text())
    # "hello and welcome" and "to the channel" each appear once despite VTT repetition
    assert out.count("hello and welcome") == 1
    assert out.count("to the channel") == 1
    assert "today we install yt-dlp" in out

def test_clean_vtt_empty():
    assert clean_vtt("WEBVTT\n\n") == ""

def test_clean_vtt_keeps_timed_text_without_metadata_or_cue_identifiers():
    vtt = (
        "WEBVTT\r\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\r\n\r\n"
        "NOTE caption metadata\r\n\r\n"
        "cue-1\r\n00:00.000 --> 00:03.000 align:start\r\nhello\r\n"
    )
    assert clean_vtt(vtt) == "hello"
