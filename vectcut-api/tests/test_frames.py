# Added in capcut-mcp-kit (2026): frames come from the right moment of the right file, for times of a
# file and of a timeline (cuts, reordered pieces, speed, cloud placeholder paths, gaps, missing
# files), within the limits. See NOTICE at the repository root.
import base64
import json
import shutil
import subprocess

import pytest

import pyJianYingDraft as draft
from pyJianYingDraft import trange

import frames

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture
def clip(tmp_path):
    """4 s of video: red for the first 2 s, blue for the last 2."""
    path = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=red:s=320x180:d=2:r=25",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=2:r=25",
                    "-filter_complex", "[0][1]concat=n=2:v=1", "-pix_fmt", "yuv420p", str(path)], check=True)
    return str(path)


def colour(b64: str) -> str:
    """'red' or 'blue': the dominant channel of the frame's average colour."""
    proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-vf", "scale=1:1",
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                          input=base64.b64decode(b64), capture_output=True, check=True)
    r, _, b = proc.stdout[:3]
    return "red" if r > b else "blue"


def jpeg_width(b64: str) -> int:
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width", "-of", "csv=p=0", "pipe:0"],
                          input=base64.b64decode(b64), capture_output=True, check=True)
    return int(proc.stdout.strip())


def make_draft(clip, pieces, speed=None):
    """A draft whose video_main plays pieces (timeline start, source start, source length) of clip."""
    from create_draft import create_draft
    script, draft_id = create_draft()
    script.add_track(draft.Track_type.video, track_name="video_main")
    material = draft.Video_material(material_type="video", remote_url=clip, material_name="talk.mp4",
                                    duration=4, width=0, height=0)
    for at, src, length in pieces:
        seg = draft.Video_segment(material, target_timerange=trange(f"{at}s", f"{length / (speed or 1)}s"),
                                  source_timerange=trange(f"{src}s", f"{length}s"), speed=speed)
        script.add_segment(seg, track_name="video_main")
    return draft_id


def test_times_of_a_file(clip):
    out = frames.view_frames(path=clip, times=[0.5, 3.5])
    assert out["timeline"] is False and out["of"] == "talk.mp4"
    assert [colour(f["image"]) for f in out["frames"]] == ["red", "blue"]
    assert jpeg_width(out["frames"][0]["image"]) == 360


def test_every_covers_the_file_and_past_the_end_is_a_note(clip):
    out = frames.view_frames(path=clip, every=1, width=160)
    assert [f["time"] for f in out["frames"]] == [0, 1, 2, 3, 4]
    assert [colour(f["image"]) for f in out["frames"]] == ["red", "red", "blue", "blue", "blue"]
    assert out["frames"][-1]["source_time"] == 3.95  # the file's end shows its last frame
    assert jpeg_width(out["frames"][0]["image"]) == 160
    late = frames.view_frames(path=clip, times=[30])["frames"][0]
    assert late == {"time": 30, "note": "past the end of the file (4.0s)"}


def test_limits():
    with pytest.raises(ValueError, match="at most 24 per call, so use every=0.44"):
        frames.frame_times(every=0.1, start=0, end=10)
    with pytest.raises(ValueError, match="either times or every"):
        frames.frame_times(times=[1], every=1, end=4)
    with pytest.raises(ValueError, match="at most 24"):
        frames.frame_times(times=list(range(25)))
    with pytest.raises(ValueError, match="width"):
        frames.view_frames(path="/x.mp4", times=[0], width=2000)
    with pytest.raises(ValueError, match="either path"):
        frames.view_frames(path="/x.mp4", draft_id="d", times=[0])
    assert frames.frame_times(every=2, start=1, end=5) == [1, 3, 5]


def test_times_of_the_timeline_follow_the_cuts(clip):
    # The blue half first, then the red half: the timeline reverses the file
    draft_id = make_draft(clip, [(0, 2, 2), (2, 0, 2)])
    out = frames.view_frames(draft_id=draft_id, times=[1, 3, 4])
    assert out["timeline"] is True and out["of"] == 'track "video_main"'
    got = out["frames"]
    assert [colour(f["image"]) for f in got] == ["blue", "red", "red"]
    # The end of a piece shows its own last frame, not the first frame of what was cut after it
    assert [f["source_time"] for f in got] == [3.0, 1.0, 1.95]
    assert got[0]["source"] == "talk.mp4"


def test_gaps_and_speed_on_the_timeline(clip):
    # Source 2-4 s (blue) played at 2x from 0 to 1 s, then nothing until 5 s, then source 0-2 s at 2x
    draft_id = make_draft(clip, [(0, 2, 2), (5, 0, 2)], speed=2)
    got = frames.view_frames(draft_id=draft_id, times=[0.5, 3, 5.25])["frames"]
    assert got[0]["source_time"] == 3.0 and colour(got[0]["image"]) == "blue"
    assert "image" not in got[1] and "no clip" in got[1]["note"]
    assert got[2]["source_time"] == 0.5 and colour(got[2]["image"]) == "red"


def test_every_on_the_timeline_stops_at_the_track_end(clip):
    draft_id = make_draft(clip, [(0, 0, 3)])
    assert [f["time"] for f in frames.view_frames(draft_id=draft_id, every=1)["frames"]] == [0, 1, 2, 3]
    with pytest.raises(ValueError, match="Video track nope not found"):
        frames.view_frames(draft_id=draft_id, times=[0], track_name="nope")


def project(tmp_path, clip, speed=None, path=None):
    """A project opened from CapCut whose main track plays the file's blue half then its red half."""
    import draft_store
    folder = tmp_path / "Progetto"
    (folder / "materials").mkdir(parents=True)
    shutil.copy(clip, folder / "materials" / "talk.mp4")
    speeds = [{"id": "SPEED-1", "type": "speed", "speed": speed}] if speed else []
    refs = ["SPEED-1"] if speed else []
    seg = lambda sid, at, src, dur: {"id": sid, "material_id": "MAT-V1", "extra_material_refs": refs,
                                     "target_timerange": {"start": at, "duration": dur},
                                     "source_timerange": {"start": src, "duration": dur * (speed or 1)}}
    content = {"materials": {"videos": [{"id": "MAT-V1", "type": "video", "duration": 4_000_000,
                                         "path": path or "##_draftpath_placeholder_ABC-123_##/materials/talk.mp4"}],
                             "speeds": speeds, "texts": [{"id": "MAT-T1", "content": "{}"}]},
               "tracks": [{"type": "video", "segments": []},
                          {"type": "text", "segments": [{"id": "TXT", "material_id": "MAT-T1",
                                                         "target_timerange": {"start": 0, "duration": 1_000_000}}]},
                          {"type": "video", "segments": [seg("SEG-1", 0, 2_000_000, 1_000_000),
                                                         seg("SEG-2", 1_000_000, 0, 1_000_000)]}]}
    script = draft.Script_file(1080, 1920, 30)
    script.base_project = {"dir": str(folder), "name": "Progetto", "raw": json.dumps(content)}
    draft_id = f"dfd_test_{folder.parent.name}"
    draft_store.store_new(draft_id, script)
    return draft_id


def test_opened_project_uses_its_main_track_and_cloud_paths(tmp_path, clip):
    draft_id = project(tmp_path, clip)
    out = frames.view_frames(draft_id=draft_id, times=[0.5, 1.5])
    assert out["of"] == "project track main"
    assert [colour(f["image"]) for f in out["frames"]] == ["blue", "red"]
    assert [f["source_time"] for f in out["frames"]] == [2.5, 0.5]
    # The empty first video track is CapCut's main track, but it shows nothing: by index it is empty
    assert "no clip" in frames.view_frames(draft_id=draft_id, times=[0.5], project_track=0)["frames"][0]["note"]
    with pytest.raises(ValueError, match="project_track"):
        frames.view_frames(draft_id=draft_id, times=[0.5], project_track=1)


def test_opened_project_speed_and_missing_files(tmp_path, clip):
    got = frames.view_frames(draft_id=project(tmp_path, clip, speed=2), times=[0.25])["frames"][0]
    assert got["source_time"] == 2.5 and colour(got["image"]) == "blue"
    gone = frames.view_frames(draft_id=project(tmp_path / "b", clip, path="/nowhere/talk.mp4"), times=[0.5])
    assert gone["frames"][0] == {"time": 0.5, "source": "talk.mp4", "note": "the clip's file is not on this Mac"}
