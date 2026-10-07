# Modified in capcut-mcp-kit (2026) from VectCutAPI @ cfa4779: keyframes are validated against the track's clips before any is queued, so a bad one is an error and nothing is half-applied; values are checked like those for existing clips (finite, in range).
# See NOTICE at the repository root.
import pyJianYingDraft as draft
from clip_edits import keyframe_value
from pyJianYingDraft import exceptions
from create_draft import get_or_create_draft
from typing import Optional, Dict, List

from util import generate_draft_url

def add_video_keyframe_impl(
    draft_id: Optional[str] = None,
    track_name: str = "main",
    property_type: str = "alpha",
    time: float = 0.0,
    value: str = "1.0",
    property_types: Optional[List[str]] = None,
    times: Optional[List[float]] = None,
    values: Optional[List[str]] = None
) -> Dict[str, str]:
    """
    Add keyframes to the specified segment
    :param draft_id: Draft ID, if None or corresponding zip file not found, a new draft will be created
    :param track_name: Track name, default "main"
    :param property_type: Keyframe property type, supports the following values:
        - position_x: Horizontal position, range [-1,1], 0 means center, 1 means rightmost
        - position_y: Vertical position, range [-1,1], 0 means center, 1 means bottom
        - rotation: Clockwise rotation angle
        - scale_x: X-axis scale ratio (1.0 means no scaling), mutually exclusive with uniform_scale
        - scale_y: Y-axis scale ratio (1.0 means no scaling), mutually exclusive with uniform_scale
        - uniform_scale: Overall scale ratio (1.0 means no scaling), mutually exclusive with scale_x and scale_y
        - alpha: Opacity, 1.0 means completely opaque
        - saturation: Saturation, 0.0 means original saturation, range from -1.0 to 1.0
        - contrast: Contrast, 0.0 means original contrast, range from -1.0 to 1.0
        - brightness: Brightness, 0.0 means original brightness, range from -1.0 to 1.0
        - volume: Volume, 1.0 means original volume
    :param time: Keyframe time point (seconds), default 0.0
    :param value: Keyframe value, format varies according to property_type:
        - position_x/position_y: "0" means center position, range [-1,1]
        - rotation: "45deg" means 45 degrees
        - scale_x/scale_y/uniform_scale: "1.5" means scale up by 1.5 times
        - alpha: "50%" means 50% opacity
        - saturation/contrast/brightness: "+0.5" means increase by 0.5, "-0.5" means decrease by 0.5
        - volume: "80%" means 80% of original volume
    :param property_types: Batch mode: List of keyframe property types, e.g. ["alpha", "position_x", "rotation"]
    :param times: Batch mode: List of keyframe time points (seconds), e.g. [0.0, 1.0, 2.0]
    :param values: Batch mode: List of keyframe values, e.g. ["1.0", "0.5", "45deg"]
    Note: property_types, times, values must be provided together and have equal lengths. If these parameters are provided, single keyframe parameters will be ignored
    :return: Updated draft information
    """
    # Get or create draft
    draft_id, script = get_or_create_draft(
        draft_id=draft_id
    )
    
    try:
        # Get specified track
        track = script.get_track(draft.Video_segment, track_name=track_name)
        
        # Get segments in the track
        segments = track.segments
        if not segments:
            raise Exception(f"No segments in track {track_name}")
        
        # Determine the keyframes list to process
        if property_types is not None or times is not None or values is not None:
            # Batch mode: use three array parameters
            if property_types is None or times is None or values is None:
                raise Exception("In batch mode, property_types, times, values must be provided together")
            
            if not (isinstance(property_types, list) and isinstance(times, list) and isinstance(values, list)):
                raise Exception("property_types, times, values must all be list types")
            
            if len(property_types) == 0:
                raise Exception("In batch mode, parameter lists cannot be empty")
            
            if not (len(property_types) == len(times) == len(values)):
                raise Exception(f"property_types, times, values must have equal lengths, current lengths are: {len(property_types)}, {len(times)}, {len(values)}")
            
            keyframes_to_process = [
                {
                    "property_type": prop_type,
                    "time": t,
                    "value": val
                }
                for prop_type, t, val in zip(property_types, times, values)
            ]
        else:
            # Single mode: use original parameters
            keyframes_to_process = [{
                "property_type": property_type,
                "time": time,
                "value": value
            }]
        
        # Validate all keyframes first, so a bad one leaves the draft unchanged
        for i, kf in enumerate(keyframes_to_process):
            try:
                _validate_keyframe(track, kf["property_type"], kf["time"], kf["value"])
            except Exception as e:
                raise Exception(f"Keyframe #{i+1} (property_type={kf['property_type']}, time={kf['time']}, value={kf['value']}): {str(e)}")
        for kf in keyframes_to_process:
            track.add_pending_keyframe(kf["property_type"], kf["time"], kf["value"])
        added_count = len(keyframes_to_process)
        
        result = {
            "draft_id": draft_id,
            "draft_url": generate_draft_url(draft_id)
        }
        
        # If in batch mode, return the number of added keyframes
        if property_types is not None:
            result["added_keyframes_count"] = added_count
        
        return result
        
    except exceptions.TrackNotFound:
        raise Exception(f"Track named {track_name} not found")
    except Exception as e:
        raise Exception(f"Failed to add keyframe: {str(e)}")


def _validate_keyframe(track, property_type: str, time: float, value: str):
    """
    Check a keyframe can be applied: known property, valid value, and a clip on the track at that time
    """
    # Convert property type string to enum value, validate if property type is valid
    try:
        property_enum = getattr(draft.Keyframe_property, property_type)
    except:
        raise Exception(f"Unsupported keyframe property type: {property_type}")
        
    # The value, whatever its form ("50%", "45deg", "+0.2"), must be a finite number in the property's range
    try:
        keyframe_value(property_type, value)
    except ValueError as e:
        raise Exception(f"Invalid value {value}: {e}")

    target_time = int(float(time) * 1000000)
    if not any(seg.target_timerange.start <= target_time <= seg.target_timerange.end for seg in track.segments):
        raise Exception(f"No clip on track '{track.name}' at {time}s: add the clip first, or use a time inside it")