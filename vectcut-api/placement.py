# Added in capcut-mcp-kit (2026): collision-free placement. See NOTICE at the repository root.
"""
When a route runs with auto_track on (the default), an item that would overlap another one on its
track goes to the first track of the same kind that is free at that time ("text_main_2", ...,
stacked just above), instead of failing. Each such move is recorded and reported in the reply.
"""

from contextvars import ContextVar

AUTO_TRACK: ContextVar[bool] = ContextVar("auto_track", default=False)
MOVED: ContextVar[list] = ContextVar("moved_to_free_track", default=None)

MAX_EXTRA_TRACKS = 20


def free_track(script, target, segment):
    """The track to use instead of `target` (which overlaps `segment`), created if needed."""
    for i in range(2, MAX_EXTRA_TRACKS + 2):
        name = f"{target.name}_{i}"
        track = script.tracks.get(name)
        if track is None:
            script.add_track(target.track_type, track_name=name, absolute_index=target.render_index + i - 1)
            track = script.tracks[name]
        elif track.track_type != target.track_type:
            continue
        if not any(seg.overlaps(segment) for seg in track.segments):
            moved = MOVED.get()
            if moved is not None:
                moved.append({"requested_track": target.name, "track": name,
                              "start": round(segment.target_timerange.start / 1e6, 3)})
            return track
    raise ValueError(f"No free track like {target.name} at that time (tried {MAX_EXTRA_TRACKS})")
