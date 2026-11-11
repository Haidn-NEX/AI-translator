import argparse
import csv
import glob
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return float(s[idx])


def parse_summary_csvs(csv_paths: list[str]) -> dict[str, list[dict]]:
    by_mode: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(csv_paths):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    mode = (row.get("mode") or "unknown").strip()
                    row["_file"] = os.path.basename(path)
                    by_mode[mode].append(row)
        except Exception as e:
            print(f"[Cảnh báo] Không đọc được {path}: {e}", file=sys.stderr)
    return by_mode


def compute_runs_per_min(rows: list[dict]) -> float:
    by_file: dict[str, list[datetime]] = defaultdict(list)
    for r in rows:
        ts_str = (r.get("ts") or "").strip()
        fname = r.get("_file", "")
        if not ts_str:
            continue
        try:
            dt = datetime.fromisoformat(ts_str)
            by_file[fname].append(dt)
        except ValueError:
            pass

    total_runs = len(rows)
    total_minutes = 0.0
    for _, dts in by_file.items():
        if len(dts) >= 2:
            span_sec = max(0.0, (max(dts) - min(dts)).total_seconds())
            total_minutes += max(span_sec / 60.0, 0.5)
        elif len(dts) == 1:
            total_minutes += 1.0

    if total_minutes <= 0.0:
        return float(total_runs)
    return total_runs / total_minutes


def parse_app_debug_log(log_path: str) -> dict[str, list[int]]:
    """
    Đọc app_debug.log để bóc tách trans_ms khi không preempt vs khi có (preempted_summary),
    cũng như số lần dropped_stale.
    """
    result = {
        "trans_normal": [],
        "trans_preempted": [],
        "trans_all": [],
        "dropped_stale_events": 0,
    }
    if not os.path.isfile(log_path):
        return result

    re_trans = re.compile(r"trans=(\d+)ms(\s*\(preempted_summary\))?")
    re_drop = re.compile(r"dropped_stale=(\d+)")

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re_trans.search(line)
                if m:
                    ms = int(m.group(1))
                    is_preempted = bool(m.group(2))
                    result["trans_all"].append(ms)
                    if is_preempted:
                        result["trans_preempted"].append(ms)
                    else:
                        result["trans_normal"].append(ms)
                if re_drop.search(line):
                    result["dropped_stale_events"] += 1
    except Exception as e:
        print(f"[Cảnh báo] Không đọc được {log_path}: {e}", file=sys.stderr)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Tổng hợp telemetry CSV của Thư ký Hợp nhất Ý chính & app_debug.log"
    )
    parser.add_argument(
        "--logs-dir",
        type=str,
        default=os.path.join(ROOT_DIR, "meeting_logs"),
        help="Thư mục chứa summary_metrics_*.csv (mặc định: meeting_logs)",
    )
    parser.add_argument(
        "--debug-log",
        type=str,
        default=os.path.join(ROOT_DIR, "app_debug.log"),
        help="Đường dẫn file app_debug.log",
    )
    args = parser.parse_args()

    pattern = os.path.join(args.logs_dir, "summary_metrics_*.csv")
    csv_files = glob.glob(pattern)
    by_mode = parse_summary_csvs(csv_files)
    log_stats = parse_app_debug_log(args.debug_log)

    print("### 1. Bảng So Sánh Telemetry Tóm Tắt Ý Chính (`summary_metrics_*.csv`)\n")
    if not by_mode:
        print("> Chưa tìm thấy dữ liệu trong `meeting_logs/summary_metrics_*.csv`.\n")
    else:
        print(
            "| `mode` | Số lượt | Lần chạy / phút | `total_ms` p50 | `total_ms` p95 | "
            "Tỉ lệ `ok` | Tỉ lệ `preempted` | TB `new_points` / lượt | TB `gap_since_last_s` |"
        )
        print("| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for mode in sorted(by_mode.keys()):
            rows = by_mode[mode]
            n = len(rows)
            runs_per_min = compute_runs_per_min(rows)
            totals = [float(r.get("total_ms") or 0) for r in rows if (r.get("total_ms") or "").strip()]
            ok_cnt = sum(1 for r in rows if str(r.get("ok", "")).strip() == "1")
            preempt_cnt = sum(1 for r in rows if str(r.get("preempted", "")).strip() == "1")
            new_pts = [
                float(r.get("new_points") or 0)
                for r in rows
                if str(r.get("ok", "")).strip() == "1"
            ]
            gaps = [
                float(r.get("gap_since_last_s") or 0)
                for r in rows
                if float(r.get("gap_since_last_s") or 0) > 0
            ]

            p50_ms = int(round(percentile(totals, 50))) if totals else 0
            p95_ms = int(round(percentile(totals, 95))) if totals else 0
            ok_rate = (ok_cnt * 100.0 / n) if n else 0.0
            preempt_rate = (preempt_cnt * 100.0 / n) if n else 0.0
            avg_new_pts = (sum(new_pts) / len(new_pts)) if new_pts else 0.0
            avg_gap = (sum(gaps) / len(gaps)) if gaps else 0.0

            print(
                f"| `{mode}` | {n} | {runs_per_min:.2f} | {p50_ms} ms | {p95_ms} ms | "
                f"{ok_rate:.1f}% | {preempt_rate:.1f}% | {avg_new_pts:.2f} | {avg_gap:.1f}s |"
            )
        print()

    print("### 2. Độ Trễ Dịch Phụ Đề (`trans_ms` từ `app_debug.log`)\n")
    print("| Nhóm lượt dịch | Số mẫu | `trans_ms` p50 | `trans_ms` p95 |")
    print("| :--- | ---: | ---: | ---: |")
    for label, key in [
        ("Không đụng độ tóm tắt (`normal`)", "trans_normal"),
        ("Có cắt ngang tóm tắt (`preempted_summary`)", "trans_preempted"),
        ("Tổng hợp toàn bộ (`all`)", "trans_all"),
    ]:
        vals = log_stats[key]
        p50_v = int(round(percentile(vals, 50))) if vals else 0
        p95_v = int(round(percentile(vals, 95))) if vals else 0
        print(f"| {label} | {len(vals)} | {p50_v} ms | {p95_v} ms |")
    print(f"\n- **Số sự kiện bỏ đoạn cũ (`dropped_stale`):** `{log_stats['dropped_stale_events']}`")


if __name__ == "__main__":
    main()

