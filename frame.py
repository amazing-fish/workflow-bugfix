# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from rosbags.highlevel import AnyReader


class MultiFrameDecoder:
    """
    多帧解码器。

    输入：
    - bag_paths
    - target_ts
    - out_dir
    - sampling / decoder / match

    输出：
    - out_dir/manifest.json
    - out_dir/sample01~sampleNN/frames/<camera>.png
    """

    def __init__(self, config: dict[str, Any]):
        input_cfg = config.get("input", {})
        output_cfg = config.get("output", {})
        sampling_cfg = config.get("sampling", {})
        decoder_cfg = config.get("decoder", {})
        match_cfg = config.get("match", {})

        self.bag_paths = [Path(p) for p in input_cfg.get("bag_paths", [])]
        self.target_ts = float(input_cfg["target_ts"])
        self.out_dir = Path(output_cfg.get("out_dir", "output"))

        self.before_frames = int(sampling_cfg.get("before_frames", 4))
        self.after_frames = int(sampling_cfg.get("after_frames", 5))

        self.ffmpeg_path = decoder_cfg.get("ffmpeg_path", "ffmpeg")
        self.loglevel = decoder_cfg.get("loglevel", "error")
        self.image_ext = decoder_cfg.get("image_ext", "png").lstrip(".").lower()
        self.context_before_packets = int(decoder_cfg.get("context_before_packets", 12))
        self.context_after_packets = int(decoder_cfg.get("context_after_packets", 0))

        self.topic_keywords = match_cfg.get(
            "topic_keywords", ["camera", "encoded", "h265", "fisheye"]
        )
        self.payload_fields = match_cfg.get(
            "payload_fields", ["data", "raw_data", "payload"]
        )

        if not self.bag_paths:
            raise ValueError("input.bag_paths 不能为空")

        self.out_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> dict[str, Any]:
        manifest = {
            "target_ts": self.target_ts,
            "before_frames": self.before_frames,
            "after_frames": self.after_frames,
            "total_frames_each_camera": self.before_frames + 1 + self.after_frames,
            "bags": {},
        }

        for bag_path in self.bag_paths:
            camera_name = bag_path.stem

            if not bag_path.exists():
                manifest["bags"][camera_name] = {
                    "status": "bag_not_found",
                    "bag_path": str(bag_path),
                    "frames": [],
                }
                print(f"[WARN] {camera_name}: bag 不存在")
                continue

            manifest["bags"][camera_name] = self._process_one_bag(bag_path)

        manifest_path = self.out_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        summary = self._build_summary(manifest)
        return {
            "manifest_path": str(manifest_path),
            "out_dir": str(self.out_dir),
            "summary": summary,
            "manifest": manifest,
        }

    def _process_one_bag(self, bag_path: Path) -> dict[str, Any]:
        with AnyReader([bag_path]) as reader:
            connections = self._select_connections(reader, bag_path)
            if not connections:
                return {
                    "status": "no_matched_topic",
                    "bag_path": str(bag_path),
                    "frames": [],
                }

            indexed = self._build_index(reader, connections)
            if not indexed:
                return {
                    "status": "no_messages",
                    "bag_path": str(bag_path),
                    "frames": [],
                }

            ts_list = [x["ts_sec"] for x in indexed]
            center_idx = self._find_nearest_index(ts_list, self.target_ts)
            selected, center_slot = self._select_fixed_window_indices(
                total=len(indexed),
                center_idx=center_idx,
                before_frames=self.before_frames,
                after_frames=self.after_frames,
            )

            center_actual_ts = indexed[selected[center_slot][1]]["ts_sec"]
            frames = []
            for slot, frame_idx in selected:
                item = indexed[frame_idx]
                actual_ts = item["ts_sec"]
                sample_no = slot + 1
                sample_name = f"sample{sample_no:02d}"
                sample_frames_dir = self.out_dir / sample_name / "frames"
                sample_frames_dir.mkdir(parents=True, exist_ok=True)
                output_name = f"{bag_path.stem}.{self.image_ext}"
                output_path = sample_frames_dir / output_name

                decode_result = self._decode_one_frame(reader, indexed, frame_idx, output_path)
                frames.append({
                    "sample": sample_name,
                    "sample_dir": str(self.out_dir / sample_name),
                    "frame_idx": frame_idx,
                    "actual_ts": round(actual_ts, 9),
                    "delta_to_target_sec": round(actual_ts - self.target_ts, 9),
                    "delta_to_center_sec": round(actual_ts - center_actual_ts, 9),
                    "status": decode_result["status"],
                    "image_path": str(output_path) if decode_result["status"] == "ok" else None,
                    "decode_method": decode_result.get("decode_method"),
                    "payload_field": decode_result.get("payload_field"),
                })

            ok_count = sum(1 for x in frames if x["status"] == "ok")
            print(
                f"[OK] {bag_path.stem}: "
                f"target={self.target_ts:.9f}, "
                f"center={center_actual_ts:.9f}, "
                f"center_delta={center_actual_ts - self.target_ts:+.9f}, "
                f"decoded={ok_count}/{len(frames)}"
            )

            center_sample_name = f"sample{center_slot + 1:02d}"
            return {
                "status": "ok" if ok_count > 0 else "decode_failed",
                "bag_path": str(bag_path),
                "center_actual_ts": round(center_actual_ts, 9),
                "center_delta_sec": round(center_actual_ts - self.target_ts, 9),
                "center_sample": center_sample_name,
                "relative_offsets_sec": [x["delta_to_center_sec"] for x in frames],
                "target_deltas_sec": [x["delta_to_target_sec"] for x in frames],
                "frames": frames,
            }

    def _select_connections(self, reader: AnyReader, bag_path: Path):
        stem = bag_path.stem.lower()

        exact = [c for c in reader.connections if c.topic.lower() == stem]
        if exact:
            return exact

        contains = [c for c in reader.connections if stem in c.topic.lower()]
        if contains:
            return contains

        def topic_match(topic: str) -> bool:
            t = topic.lower()
            return "camera" in t and any(k.lower() in t for k in self.topic_keywords)

        return [c for c in reader.connections if topic_match(c.topic)]

    def _build_index(self, reader: AnyReader, connections) -> list[dict[str, Any]]:
        indexed = []
        for i, (conn, timestamp_ns, rawdata) in enumerate(reader.messages(connections=connections)):
            indexed.append({
                "msg_idx": i,
                "ts_sec": timestamp_ns / 1e9,
                "conn": conn,
                "rawdata": rawdata,
            })
        indexed.sort(key=lambda x: x["ts_sec"])
        return indexed

    @staticmethod
    def _find_nearest_index(ts_list: list[float], target_ts: float) -> int:
        best_idx = 0
        best_delta = abs(ts_list[0] - target_ts)
        for i in range(1, len(ts_list)):
            d = abs(ts_list[i] - target_ts)
            if d < best_delta:
                best_idx = i
                best_delta = d
        return best_idx

    @staticmethod
    def _select_fixed_window_indices(
        total: int,
        center_idx: int,
        before_frames: int,
        after_frames: int,
    ) -> tuple[list[tuple[int, int]], int]:
        """
        Returns ([(slot, frame_idx), ...], center_slot).
        slot is 0-based sequential; center_slot marks which slot holds center_idx.
        """
        window_size = before_frames + 1 + after_frames
        start = center_idx - before_frames
        end = center_idx + after_frames

        if start < 0:
            shift = -start
            start += shift
            end += shift

        if end >= total:
            shift = end - (total - 1)
            start -= shift
            end -= shift

        start = max(0, start)
        end = min(total - 1, end)
        indices = list(range(start, end + 1))

        while len(indices) < window_size:
            if indices and indices[0] > 0:
                indices.insert(0, indices[0] - 1)
            elif indices and indices[-1] < total - 1:
                indices.append(indices[-1] + 1)
            else:
                break

        indices = indices[:window_size]

        if center_idx in indices:
            center_slot = indices.index(center_idx)
        else:
            center_slot = min(range(len(indices)), key=lambda i: abs(indices[i] - center_idx))

        if len(indices) < window_size:
            print(
                f"[WARN] 帧数不足: 需要 {window_size} 帧，实际仅 {len(indices)} 帧 "
                f"(total={total}, center_idx={center_idx})，"
                f"sample 编号保持连续槽位，真实偏移见 delta_to_center_sec"
            )
        pairs = [(slot, indices[slot]) for slot in range(len(indices))]
        return pairs, center_slot

    def _find_payload(self, msg) -> tuple[bytes | None, str | None]:
        for field in self.payload_fields:
            if hasattr(msg, field):
                value = getattr(msg, field)
                if value is not None:
                    try:
                        return bytes(value), field
                    except Exception:
                        pass
        return None, None

    def _decode_one_frame(
        self,
        reader: AnyReader,
        indexed: list[dict[str, Any]],
        frame_idx: int,
        output_path: Path,
    ) -> dict[str, Any]:
        entry = indexed[frame_idx]
        msg = reader.deserialize(entry["rawdata"], entry["conn"].msgtype)
        payload, used_field = self._find_payload(msg)
        if payload is None:
            return {"status": "payload_not_found"}

        if self._decode_single_packet(payload, output_path):
            return {
                "status": "ok",
                "decode_method": "single_packet",
                "payload_field": used_field,
            }

        if self._decode_with_context_stream(reader, indexed, frame_idx, output_path):
            return {
                "status": "ok",
                "decode_method": "context_stream",
                "payload_field": used_field,
            }

        return {
            "status": "decode_failed",
            "payload_field": used_field,
        }

    def _decode_single_packet(self, payload: bytes, output_path: Path) -> bool:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            h265_file = tmpdir_path / "single.h265"
            h265_file.write_bytes(payload)
            cmd = [
                self.ffmpeg_path,
                "-y",
                "-loglevel", self.loglevel,
                "-i", str(h265_file),
                "-frames:v", "1",
                str(output_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            return proc.returncode == 0 and output_path.exists()

    def _decode_with_context_stream(
        self,
        reader: AnyReader,
        indexed: list[dict[str, Any]],
        frame_idx: int,
        output_path: Path,
    ) -> bool:
        start_idx = max(0, frame_idx - self.context_before_packets)
        end_idx = min(len(indexed) - 1, frame_idx + self.context_after_packets)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            stream_file = tmpdir_path / "stream.h265"
            frames_dir = tmpdir_path / "frames"
            frames_dir.mkdir(parents=True, exist_ok=True)

            with open(stream_file, "wb") as f:
                for i in range(start_idx, end_idx + 1):
                    entry = indexed[i]
                    msg = reader.deserialize(entry["rawdata"], entry["conn"].msgtype)
                    payload, _ = self._find_payload(msg)
                    if payload:
                        f.write(payload)

            out_pattern = frames_dir / f"frame_%04d.{self.image_ext}"
            cmd = [
                self.ffmpeg_path,
                "-y",
                "-loglevel", self.loglevel,
                "-i", str(stream_file),
                str(out_pattern),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)

            generated = sorted(frames_dir.glob(f"frame_*.{self.image_ext}"))
            if proc.returncode == 0 and generated:
                shutil.copyfile(generated[-1], output_path)
                return True
            return False

    @staticmethod
    def _build_summary(manifest: dict[str, Any]) -> dict[str, Any]:
        bags = manifest.get("bags", {})
        total_bags = len(bags)
        ok_bags = 0
        total_frames = 0
        ok_frames = 0

        bag_summaries = {}
        for camera_name, bag_info in bags.items():
            frames = bag_info.get("frames", [])
            bag_ok_frames = sum(1 for x in frames if x.get("status") == "ok")
            bag_total_frames = len(frames)
            if bag_info.get("status") in ("ok", "decode_failed") and bag_ok_frames > 0:
                ok_bags += 1
            total_frames += bag_total_frames
            ok_frames += bag_ok_frames
            bag_summaries[camera_name] = {
                "status": bag_info.get("status"),
                "ok_frames": bag_ok_frames,
                "total_frames": bag_total_frames,
            }

        expected_frames_per_bag = manifest.get("total_frames_each_camera", 0)
        expected_frames = total_bags * expected_frames_per_bag

        return {
            "expected_bags": total_bags,
            "ok_bags": ok_bags,
            "expected_frames": expected_frames,
            "ok_frames": ok_frames,
            "total_bags": total_bags,
            "total_frames": total_frames,
            "bags": bag_summaries,
        }


def load_config(config_path: str | Path) -> dict[str, Any]:
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("用法: python frame.py decoder_config.json")
        raise SystemExit(1)

    cfg = load_config(sys.argv[1])
    decoder = MultiFrameDecoder(cfg)
    decoder.run()
