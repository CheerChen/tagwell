"""Tests for MusicBrainz patch planning and writing."""

from __future__ import annotations

from pathlib import Path

import tagwell.patch as patch_module
from tagwell.patch import PatchTarget, ReleaseInfo


def _snapshot(
    *,
    relative_path: str = "Album/01 - Song.mp3",
    tag_format: str = "id3",
    release_id: str | None = "release-1",
    release_track_id: str | None = "track-1",
    recording_id: str | None = None,
    raw: dict[str, list[str]] | None = None,
) -> dict:
    return {
        "record_type": "audio_file_snapshot",
        "file": {"relative_path": relative_path},
        "tags": {
            "format": tag_format,
            "raw": raw or {},
            "external_ids": {
                "musicbrainz": {
                    "recording_id": recording_id,
                    "release_id": release_id,
                    "release_group_id": "group-1",
                    "release_track_id": release_track_id,
                    "artist_ids": [],
                    "album_artist_ids": [],
                }
            },
        },
    }


def test_recording_id_targets_require_release_and_release_track_ids():
    header = {"scan": {"root": "/music"}}
    targets = patch_module.find_patch_targets(
        header,
        [
            _snapshot(relative_path="yes.mp3"),
            _snapshot(relative_path="has-recording.mp3", recording_id="recording-1"),
            _snapshot(relative_path="missing-release.mp3", release_id=None),
            _snapshot(relative_path="missing-release-track.mp3", release_track_id=None),
        ],
        mode="recording-id",
    )

    assert [target.relative_path for target in targets] == ["yes.mp3"]
    assert targets[0].needs_recording_id is True
    assert targets[0].absolute_path == Path("/music/yes.mp3")


def test_release_payload_extracts_release_track_to_recording_mapping():
    mapping = patch_module._extract_track_recordings(
        {
            "media": [
                {
                    "tracks": [
                        {"id": "track-1", "recording": {"id": "recording-1"}},
                        {"id": "track-2", "recording": {"id": "recording-2"}},
                        {"id": "track-3"},
                    ]
                }
            ]
        }
    )

    assert mapping == {
        "track-1": "recording-1",
        "track-2": "recording-2",
    }


def test_dry_run_plan_reports_recording_id_field():
    target = PatchTarget(
        relative_path="Album/01 - Song.mp3",
        absolute_path=Path("/music/Album/01 - Song.mp3"),
        tag_format="id3",
        release_id="release-1",
        release_track_id="track-1",
        needs_recording_id=True,
    )
    plan = patch_module.PatchPlan(
        targets=[target],
        release_cache={"release-1": ReleaseInfo(track_recordings={"track-1": "recording-1"})},
    )

    summary, actions = patch_module.dry_run_plan(plan)

    assert summary.files_patched == 1
    assert summary.fields_written == 1
    assert actions == [("Album/01 - Song.mp3", ["recording_id = recording-1"])]


def test_build_plan_fetches_recordings_for_recording_id_mode(tmp_path, monkeypatch):
    jsonl_path = tmp_path / "library.jsonl"
    jsonl_path.write_text(
        "\n".join(
            [
                '{"record_type":"scan_header","scan":{"root":"/music"}}',
                '{"record_type":"audio_file_snapshot","file":{"relative_path":"song.mp3"},"tags":{"format":"id3","raw":{},"external_ids":{"musicbrainz":{"recording_id":null,"release_id":"release-1","release_group_id":"group-1","release_track_id":"track-1","artist_ids":[],"album_artist_ids":[]}}}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, bool]] = []

    def fake_fetch(release_id: str, *, include_recordings: bool = False) -> ReleaseInfo:
        calls.append((release_id, include_recordings))
        return ReleaseInfo(track_recordings={"track-1": "recording-1"})

    monkeypatch.setattr(patch_module, "fetch_release_info", fake_fetch)

    plan = patch_module.build_plan(jsonl_path, mode="recording-id", delay=0)

    assert calls == [("release-1", True)]
    assert len(plan.targets) == 1
    assert plan.targets[0].needs_recording_id is True


class FakeID3Tags:
    def __init__(self) -> None:
        self.frames: list[object] = []

    def add(self, frame: object) -> None:
        self.frames.append(frame)


class FakeMutagenFile:
    def __init__(self) -> None:
        self.tags = FakeID3Tags()
        self.saved = False

    def save(self) -> None:
        self.saved = True


def test_write_tags_writes_id3_musicbrainz_track_id(monkeypatch):
    fake_file = FakeMutagenFile()
    monkeypatch.setattr(patch_module.mutagen, "File", lambda _: fake_file)
    target = PatchTarget(
        relative_path="song.mp3",
        absolute_path=Path("/music/song.mp3"),
        tag_format="id3",
        release_id="release-1",
        release_track_id="track-1",
        needs_recording_id=True,
    )

    written = patch_module._write_tags(
        target,
        ReleaseInfo(track_recordings={"track-1": "recording-1"}),
    )

    assert written == ["recording_id"]
    assert fake_file.saved is True
    assert fake_file.tags.frames[0].desc == "MusicBrainz Track Id"
    assert fake_file.tags.frames[0].text == ["recording-1"]
