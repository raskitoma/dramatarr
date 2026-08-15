"""NAS scanner.

The library mount is read-only: nothing in this package ever creates, moves or deletes a file
under `LIBRARY_ROOT`. `safe_media_path` is the single gate through which every absolute path
into the mount is built, here and in the streaming layer.
"""

from app.services.scanner.parse import (
    SUBTITLE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    ParsedEpisode,
    file_extension,
    guess_subtitle_language,
    is_subtitle_file,
    is_video_file,
    normalize_language,
    parse_episode,
    parse_season_folder,
    parse_series_folder,
)
from app.services.scanner.probe import (
    AudioStream,
    ProbeOutcome,
    ProbeResult,
    SubtitleStream,
    VideoStream,
    compute_direct_play,
    probe,
    probe_file,
)
from app.services.scanner.scan import (
    ScanContext,
    ScanReport,
    library_root,
    relative_media_path,
    safe_media_path,
    scan_library,
)

__all__ = [
    "SUBTITLE_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "AudioStream",
    "ParsedEpisode",
    "ProbeOutcome",
    "ProbeResult",
    "ScanContext",
    "ScanReport",
    "SubtitleStream",
    "VideoStream",
    "compute_direct_play",
    "file_extension",
    "guess_subtitle_language",
    "is_subtitle_file",
    "is_video_file",
    "library_root",
    "normalize_language",
    "parse_episode",
    "parse_season_folder",
    "parse_series_folder",
    "probe",
    "probe_file",
    "relative_media_path",
    "safe_media_path",
    "scan_library",
]
