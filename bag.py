# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class DownloadError(RuntimeError):
    """下载失败，携带重试统计。"""
    def __init__(self, msg: str, attempts: int = 0, retry_reasons: list[str] | None = None, last_reason: str = "unknown"):
        super().__init__(msg)
        self.attempts = attempts
        self.retry_reasons = retry_reasons or []
        self.last_reason = last_reason


class DIBagDownloader:
    """
    行级下载器。

    职责：
    1. 解析 Excel 指定行。
    2. 从 checker 文本提取全部碰撞时间戳。
    3. 依据 issue link 解析定位信息并解析 transfer_path。
    4. 每个 Excel 行仅下载一次 4 个 bag。
    5. 在 outputs/rowN 下写入 row_meta.json。
    """

    COLLISION_TS_PATTERN = re.compile(r"【碰撞时间】\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)")

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.cfg = json.load(f)

        self.base_url = self.cfg["base_url"].rstrip("/")
        self.verify_ssl = self.cfg.get("verify_ssl", False)
        self.timeout_sec = int(self.cfg.get("timeout_sec", 30))
        self.debug = bool(self.cfg.get("debug", False))
        self.topics = list(self.cfg.get("topics", []))

        retry_cfg = self.cfg.get("download", {}).get("retry", {})
        if "max_attempts" in retry_cfg:
            self.download_max_attempts = max(1, int(retry_cfg["max_attempts"]))
        else:
            self.download_max_attempts = max(1, int(self.cfg.get("download_retry_times", 2)) + 1)
        self.download_backoff_base = float(retry_cfg.get("backoff_base_sec", 2.0))
        self.download_backoff_max = float(retry_cfg.get("backoff_max_sec", 30.0))

        self.browser_headers = dict(self.cfg.get("browser_headers", {}))
        self.excel_cfg = dict(self.cfg.get("excel", {}))
        self.output_cfg = dict(self.cfg.get("output", {}))

        self.excel_path = self.excel_cfg["path"]
        self.sheet = self.excel_cfg.get("sheet", 0)
        self.header = self.excel_cfg.get("header", 0)
        self.link_column = self.excel_cfg.get("link_column", "C")
        self.checker_column = self.excel_cfg.get("checker_column", "T")
        self.start_row = int(self.excel_cfg.get("start_row", 2))
        self.rows = self.excel_cfg.get("rows", []) or []

        self.output_root = Path(
            self.output_cfg.get("root_dir")
            or self.excel_cfg.get("output_dir")
            or "outputs"
        )

        self.session = requests.Session()
        self.common_headers = {
            "Accept-Language": "zh-CN",
            **self.browser_headers,
        }

    def _debug_print(self, msg: str):
        if self.debug:
            print(msg)

    # ---------------- public ----------------

    def run(self) -> dict[str, Any]:
        results = list(self.iter_download_rows())
        summary_path = self.output_root / "download_summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump({"rows": results}, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 下载汇总: {summary_path}")
        return {
            "output_root": str(self.output_root),
            "summary_path": str(summary_path),
            "rows": results,
        }

    def iter_download_rows(self) -> Iterator[dict[str, Any]]:
        self.output_root.mkdir(parents=True, exist_ok=True)

        df = pd.read_excel(
            self.excel_path,
            sheet_name=self.sheet,
            header=self.header,
        )

        link_idx = self._excel_col_to_index(self.link_column)
        checker_idx = self._excel_col_to_index(self.checker_column)
        selected_rows = self._resolve_target_excel_rows(df)

        print(f"[INFO] sheet={self.sheet}, 总目标行数={len(selected_rows)}")

        for excel_row in selected_rows:
            row_idx = self._excel_row_to_df_index(excel_row)
            if row_idx < 0 or row_idx >= len(df):
                print(f"[WARN] Excel 行号越界，跳过: {excel_row}")
                continue

            row = df.iloc[row_idx]
            issue_link = self._safe_get_cell_str(row, link_idx)
            checker_text = self._safe_get_cell_str(row, checker_idx)
            if not issue_link:
                print(f"[WARN] 第 {excel_row} 行 {self.link_column} 列为空，跳过")
                continue

            try:
                yield self._process_one_row(excel_row, issue_link, checker_text)
            except Exception as e:
                print(f"[ERROR] 第 {excel_row} 行处理失败: {e}")
                row_id = f"row{excel_row}"
                row_dir = self.output_root / row_id
                result = {
                    "excel_row": excel_row,
                    "row_id": row_id,
                    "status": "download_failed",
                    "failure_stage": "download",
                    "reason": "download_failed",
                    "error": str(e),
                }
                if isinstance(e, DownloadError):
                    result["download_retries"] = max(0, e.attempts - 1)
                if row_dir.exists():
                    result["row_dir"] = str(row_dir)
                    result["row_meta_path"] = str(row_dir / "row_meta.json")
                yield result

    def load_row_meta(self, row_dir: str | Path) -> dict[str, Any]:
        row_dir = Path(row_dir)
        row_meta_path = row_dir / "row_meta.json"
        if not row_meta_path.exists():
            raise FileNotFoundError(f"找不到 row_meta.json: {row_meta_path}")
        with open(row_meta_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------------- row processing ----------------

    def _process_one_row(self, excel_row: int, issue_link: str, checker_text: str) -> dict[str, Any]:
        collision_info = self._extract_collision_info(checker_text)
        locator = self._parse_issue_link(issue_link)
        dataset = self._resolve_dataset(locator)

        row_id = f"row{excel_row}"
        row_dir = self.output_root / row_id
        row_dir.mkdir(parents=True, exist_ok=True)

        downloaded = {}
        download_stats: list[dict[str, Any]] = []
        try:
            bucket = self._extract_bucket_from_obs_path(dataset["transfer_path"])
            obs_download_url = self._download_menu(bucket)

            self._debug_print(f"[DEBUG] row={row_id} transfer_path={dataset['transfer_path']}")
            self._debug_print(f"[DEBUG] row={row_id} bucket={bucket}")
            self._debug_print(f"[DEBUG] row={row_id} obs_download_url={obs_download_url}")

            for topic in self.topics:
                remote_path = self._build_topic_bag_path(
                    transfer_path=dataset["transfer_path"],
                    topic=topic,
                    add_slash_before_archive=dataset["add_slash_before_archive"],
                )
                obs_id = self._get_obs_id(
                    bucket=bucket,
                    remote_path=remote_path,
                    dataset_name=dataset.get("dataset_name"),
                )
                dl_result = self._download_by_obs_id(
                    obs_download_url=obs_download_url,
                    obs_id=obs_id,
                    save_dir=row_dir,
                    save_name=f"{topic}.bag",
                )
                downloaded[topic] = dl_result["path"]
                download_stats.append({"topic": topic, **dl_result})
                print(f"[OK] {row_id} 下载完成 {topic} -> {dl_result['path']}")
        except Exception as e:
            fail_stats = {}
            if isinstance(e, DownloadError):
                fail_stats = {"attempts": e.attempts, "retry_reasons": e.retry_reasons, "last_reason": e.last_reason}
            row_meta = {
                "excel_row": excel_row,
                "row_id": row_id,
                "issue_link": issue_link,
                "locator": locator,
                "dataset": dataset,
                "collision": collision_info,
                "downloaded": downloaded,
                "download_stats": download_stats,
                "tasks": [],
                "status": "download_failed",
                "failure_stage": "download",
                "reason": "download_error",
                "error": str(e),
                **fail_stats,
            }
            row_meta_path = row_dir / "row_meta.json"
            with open(row_meta_path, "w", encoding="utf-8") as f:
                json.dump(row_meta, f, ensure_ascii=False, indent=2)
            print(f"[WARN] {row_id} 下载失败，已写入失败态 row_meta: {row_meta_path}")
            raise

        tasks = self._build_decode_tasks(collision_info)
        row_meta = {
            "excel_row": excel_row,
            "row_id": row_id,
            "issue_link": issue_link,
            "locator": locator,
            "dataset": dataset,
            "collision": collision_info,
            "downloaded": downloaded,
            "download_stats": download_stats,
            "tasks": tasks,
            "status": "downloaded",
        }

        row_meta_path = row_dir / "row_meta.json"
        with open(row_meta_path, "w", encoding="utf-8") as f:
            json.dump(row_meta, f, ensure_ascii=False, indent=2)

        print(f"[INFO] {row_id} 就绪: timestamps={len(tasks)}, row_meta={row_meta_path}")
        download_retries = sum(max(0, s.get("attempts", 1) - 1) for s in download_stats)
        return {
            "excel_row": excel_row,
            "row_id": row_id,
            "status": "downloaded",
            "row_dir": str(row_dir),
            "row_meta_path": str(row_meta_path),
            "timestamp_count": len(tasks),
            "download_stats": download_stats,
            "download_retries": download_retries,
        }

    def _build_decode_tasks(self, collision_info: dict[str, Any]) -> list[dict[str, Any]]:
        ts_list = list(collision_info.get("collision_ts_list") or [])
        tasks = []
        for idx, ts in enumerate(ts_list, start=1):
            task_id = f"t{idx:02d}"
            tasks.append({
                "task_id": task_id,
                "target_ts": float(ts),
                "status": "pending",
            })
        return tasks

    # ---------------- Excel helpers ----------------

    def _resolve_target_excel_rows(self, df: pd.DataFrame) -> list[int]:
        if self.rows:
            return sorted(set(int(x) for x in self.rows if int(x) > 0))
        total_rows = len(df)
        excel_last_row = total_rows if self.header is None else total_rows + 1
        start = max(1, self.start_row)
        return list(range(start, excel_last_row + 1))

    def _excel_row_to_df_index(self, excel_row: int) -> int:
        return excel_row - 1 if self.header is None else excel_row - 2

    @staticmethod
    def _excel_col_to_index(col: str) -> int:
        col = col.upper().strip()
        num = 0
        for ch in col:
            num = num * 26 + (ord(ch) - ord("A") + 1)
        return num - 1

    @staticmethod
    def _cell_to_str(val) -> str:
        if pd.isna(val):
            return ""
        return str(val).strip()

    def _safe_get_cell_str(self, row: pd.Series, idx: int) -> str:
        if idx < 0 or idx >= len(row):
            return ""
        return self._cell_to_str(row.iloc[idx])

    # ---------------- collision parse ----------------

    def _extract_collision_info(self, checker_text: str) -> dict[str, Any]:
        if not checker_text:
            return {
                "status": "empty_checker_text",
                "failure_stage": "timestamp",
                "reason": "ts_not_found",
                "checker_text": "",
                "collision_ts": None,
                "collision_ts_list": [],
                "collision_ts_list_raw": [],
                "collision_ts_list_dedup": [],
            }

        matches = list(self.COLLISION_TS_PATTERN.finditer(checker_text))
        if not matches:
            return {
                "status": "collision_ts_not_found",
                "failure_stage": "timestamp",
                "reason": "ts_not_found",
                "checker_text": checker_text,
                "collision_ts": None,
                "collision_ts_list": [],
                "collision_ts_list_raw": [],
                "collision_ts_list_dedup": [],
            }

        raw_values: list[str] = []
        parsed_values: list[float] = []
        invalid_values: list[str] = []
        for m in matches:
            raw_val = m.group(1)
            raw_values.append(raw_val)
            try:
                parsed_values.append(float(raw_val))
            except Exception:
                invalid_values.append(raw_val)

        dedup_values: list[float] = []
        seen = set()
        for v in parsed_values:
            if v not in seen:
                seen.add(v)
                dedup_values.append(v)

        if not dedup_values:
            return {
                "status": "collision_ts_invalid",
                "failure_stage": "timestamp",
                "reason": "ts_invalid",
                "checker_text": checker_text,
                "collision_ts": None,
                "collision_ts_list": [],
                "collision_ts_list_raw": raw_values,
                "collision_ts_list_dedup": [],
                "invalid_values": invalid_values,
            }

        status = "ok" if len(dedup_values) == 1 else "multiple_matches"
        return {
            "status": status,
            "checker_text": checker_text,
            "collision_ts": dedup_values[0],
            "collision_ts_list": dedup_values,
            "collision_ts_list_raw": raw_values,
            "collision_ts_list_dedup": dedup_values,
            "invalid_values": invalid_values,
            "matched_count": len(matches),
        }

    # ---------------- link parse ----------------

    def _parse_issue_link(self, link: str) -> dict[str, Any]:
        parsed = urlparse(link)
        normal_qs = parse_qs(parsed.query)
        fragment_qs = {}
        if parsed.fragment and "?" in parsed.fragment:
            frag_query = parsed.fragment.split("?", 1)[1]
            fragment_qs = parse_qs(frag_query)

        merged_qs = {}
        for d in (normal_qs, fragment_qs):
            for k, v in d.items():
                merged_qs[k] = v

        def pick(key: str) -> str | None:
            values = merged_qs.get(key)
            return values[0] if values else None

        module_name = pick("moduleName")
        platform = "wise" if "e2eSimulation" in link else "tide"
        defect_id = pick("defectId")
        data_name = pick("dataName")
        seqno = pick("seqno")
        sub_seqno = pick("subSeqno")

        parse_mode = "unknown"
        if seqno and sub_seqno:
            parse_mode = "seq_subseq"
        elif defect_id:
            parse_mode = "defectId"
        elif data_name:
            parse_mode = "dataName"

        return {
            "raw_link": link,
            "platform": platform,
            "module_name": module_name,
            "defect_id": defect_id,
            "data_name": data_name,
            "seqno": seqno,
            "sub_seqno": sub_seqno,
            "parse_mode": parse_mode,
        }

    # ---------------- dataset resolve ----------------

    def _resolve_dataset(self, locator: dict[str, Any]) -> dict[str, Any]:
        if locator["parse_mode"] == "seq_subseq":
            return self._resolve_by_seqno(locator)
        if locator["parse_mode"] == "defectId":
            return self._resolve_by_event_list(locator["defect_id"], key="defectId")
        if locator["parse_mode"] == "dataName":
            return self._resolve_by_event_list(locator["data_name"], key="id")
        raise ValueError(f"链接无法解析为可用 case 标识: {locator['raw_link']}")

    def _resolve_by_seqno(self, locator: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/{locator['platform']}/v1/carjam/querySubTaskByType"
        payload = {
            "orderByTideName": True,
            "pageNum": 1,
            "pageSize": 100,
            "seqno": [locator["seqno"]],
        }
        headers = self._headers_for(locator["platform"])

        resp = self.session.post(url, json=payload, headers=headers, verify=self.verify_ssl, timeout=self.timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("list") or []
        if not items:
            raise RuntimeError(f"querySubTaskByType 返回空: seqno={locator['seqno']}")

        for item in items:
            if str(item.get("subSeqNo")) == str(locator["sub_seqno"]):
                transfer_path = item.get("carjamFilePath") or item.get("replayFilePath")
                if not transfer_path:
                    raise RuntimeError("命中 subtask，但缺少 carjamFilePath/replayFilePath")
                return {
                    "transfer_path": str(transfer_path),
                    "data_segment": str(item.get("tideName") or locator["sub_seqno"]),
                    "dataset_name": str(item.get("tideName") or locator["sub_seqno"]),
                    "add_slash_before_archive": False,
                }

        raise RuntimeError(f"在 seqno={locator['seqno']} 下未找到 subSeqNo={locator['sub_seqno']}")

    def _resolve_by_event_list(self, value: str, key: str) -> dict[str, Any]:
        url = f"{self.base_url}/siphon/v1/file/event/list"
        payload = {"pageNum": 1, "pageSize": 20, key: value}
        headers = self._headers_for("siphon")
        resp = self.session.post(url, json=payload, headers=headers, verify=self.verify_ssl, timeout=self.timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("list") or []
        if not items:
            raise RuntimeError(f"event/list 返回空: {key}={value}")

        item = items[0]
        transfer_path = str(item["transferFilePath"])
        return {
            "transfer_path": transfer_path,
            "data_segment": str(item.get("dataName") or value),
            "dataset_name": None,
            "add_slash_before_archive": True,
        }

    # ---------------- download ----------------

    def _download_menu(self, bucket_name: str) -> str:
        url = f"{self.base_url}/rivulet/v1/dataDownload/downloadMenu"
        headers = self._headers_for("rivulet")
        payload = {"bucketName": bucket_name}
        resp = self.session.post(url, json=payload, headers=headers, verify=self.verify_ssl, timeout=self.timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            raise RuntimeError(f"downloadMenu 业务返回异常: {data}")
        obs_download_url = ((data.get("data") or {}).get("obs_download_url"))
        if not obs_download_url:
            raise RuntimeError(f"downloadMenu 未返回 data.obs_download_url: {data}")
        return obs_download_url.rstrip("/")

    def _get_obs_id(self, bucket: str, remote_path: str, dataset_name: str | None = None) -> str:
        url = f"{self.base_url}/rivulet/v1/dataDownload/getObsId"
        headers = self._headers_for("rivulet")
        payload = {"files": [{"path": remote_path}], "bucket": bucket}
        if dataset_name:
            payload["datasetName"] = dataset_name
        resp = self.session.post(url, json=payload, headers=headers, verify=self.verify_ssl, timeout=self.timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        obs_id = data.get("result")
        if not obs_id:
            raise RuntimeError(f"getObsId 未返回 result: {data}")
        return str(obs_id)

    def _download_by_obs_id(self, obs_download_url: str, obs_id: str, save_dir: Path, save_name: str) -> dict[str, Any]:
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / save_name
        url = obs_download_url + "/obs/v1/files/download?opid=" + obs_id
        headers = self._headers_for("rivulet", json_body=False)
        last_error: Exception | None = None
        max_attempts = max(1, self.download_max_attempts)
        retry_reasons: list[str] = []

        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.get(url, headers=headers, verify=self.verify_ssl, timeout=self.timeout_sec)
                resp.raise_for_status()
                content = resp.content
                if self._looks_like_zip(content):
                    with zipfile.ZipFile(io.BytesIO(content)) as zf:
                        bag_members = [n for n in zf.namelist() if n.endswith(".bag")]
                        if not bag_members:
                            raise RuntimeError(f"download 返回 zip，但未找到 .bag: {save_name}")
                        extracted = zf.read(bag_members[0])
                        self._validate_bag_magic(extracted, save_name=save_name, source=f"zip_member:{bag_members[0]}")
                        save_path.write_bytes(extracted)
                else:
                    self._validate_bag_magic(content, save_name=save_name, source="raw_response")
                    save_path.write_bytes(content)
                time.sleep(1.0)
                return {
                    "path": str(save_path),
                    "status": "ok",
                    "attempts": attempt,
                    "retry_reasons": retry_reasons,
                }
            except Exception as e:
                last_error = e
                reason = self._classify_download_error(e)
                can_retry = self._is_retriable_download_error(e)
                if attempt >= max_attempts or not can_retry:
                    break
                retry_reasons.append(reason)
                sleep_sec = min(self.download_backoff_base ** attempt, self.download_backoff_max)
                print(
                    f"[WARN] {save_name} 下载异常({reason})，第 {attempt}/{max_attempts} 次失败，"
                    f"{sleep_sec:.1f}s 后重试: {e}"
                )
                time.sleep(sleep_sec)

        assert last_error is not None
        raise DownloadError(
            f"{save_name} 下载失败（共尝试 {attempt} 次）: {last_error}",
            attempts=attempt,
            retry_reasons=retry_reasons,
            last_reason=self._classify_download_error(last_error),
        )

    @staticmethod
    def _classify_download_error(exc: Exception) -> str:
        cur: BaseException | None = exc
        while cur is not None:
            if isinstance(cur, requests.Timeout):
                return "timeout"
            if isinstance(cur, requests.ConnectionError):
                return "connection_error"
            if isinstance(cur, zipfile.BadZipFile):
                return "bad_zip"
            cur = cur.__cause__ or cur.__context__
        msg = str(exc)
        if "File magic is invalid" in msg:
            return "invalid_bag_magic"
        if "未找到 .bag" in msg:
            return "zip_no_bag"
        return "unknown"

    @staticmethod
    def _looks_like_zip(content: bytes) -> bool:
        return len(content) >= 4 and content[:4] == b"PK\x03\x04"

    @staticmethod
    def _validate_bag_magic(content: bytes, save_name: str, source: str) -> None:
        if content.startswith(b"#ROSBAG"):
            return
        prefix = content[:64]
        prefix_text = prefix.decode("utf-8", errors="replace").replace("\n", "\\n").replace("\r", "\\r")
        raise RuntimeError(
            f"{save_name} 文件头校验失败: File magic is invalid. source={source}, "
            f"size={len(content)}, prefix_hex={prefix.hex()}, prefix_text={prefix_text}"
        )

    @staticmethod
    def _is_retriable_download_error(exc: Exception) -> bool:
        cur: BaseException | None = exc
        while cur is not None:
            if isinstance(cur, (requests.Timeout, requests.ConnectionError, zipfile.BadZipFile)):
                return True
            cur = cur.__cause__ or cur.__context__

        msg = str(exc)
        retriable_hints = [
            "download 返回 zip，但未找到 .bag",
            "Read timed out",
            "Connection aborted",
            "Remote end closed connection",
        ]
        return any(hint in msg for hint in retriable_hints)

    # ---------------- path / headers ----------------

    @staticmethod
    def _extract_bucket_from_obs_path(obs_path: str) -> str:
        if not obs_path.startswith("obs://"):
            raise ValueError(f"非法 obs path: {obs_path}")
        parts = obs_path.split("/")
        if len(parts) < 3:
            raise ValueError(f"非法 obs path: {obs_path}")
        return parts[2]

    @staticmethod
    def _build_topic_bag_path(transfer_path: str, topic: str, add_slash_before_archive: bool) -> str:
        if not transfer_path.startswith("obs://"):
            raise ValueError(f"transfer_path 非法: {transfer_path}")
        core = transfer_path[5:]
        if add_slash_before_archive:
            return core + "/archive/" + topic + ".bag"
        return core + "archive/" + topic + ".bag"

    def _headers_for(self, platform: str, json_body: bool = True) -> dict[str, str]:
        headers = dict(self.common_headers)
        headers["Deepdata-platform"] = platform
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: python bag.py config.json")
        sys.exit(1)

    downloader = DIBagDownloader(sys.argv[1])
    downloader.run()
