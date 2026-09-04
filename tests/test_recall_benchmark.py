from pathlib import Path

from tender_ai.config_loader import APP_ROOT
from tender_ai.recall_benchmark import load_recall_benchmark, run_recall_benchmark


def test_known_recall_benchmark_is_explicit_about_unavailable_cases():
    benchmark = load_recall_benchmark()
    assert len(benchmark.cases) == 11
    assert sum(case.region == "陕西省" for case in benchmark.cases) == 8
    assert sum(case.region != "陕西省" for case in benchmark.cases) >= 3
    assert all(
        case.availability.startswith("UNAVAILABLE")
        for case in benchmark.cases
        if case.case_id in {"sx_yichuan_150mwp_reinforcement", "sx_shaanxi_transport_highway_pv", "sx_xian_silk_rail_park"}
    )


def test_recall_benchmark_measures_real_report_and_three_non_shaanxi_groups():
    benchmark = load_recall_benchmark()
    report_path = APP_ROOT.parent / "output/sessions/search_20260831_180208_a5d128ac/search_report.md"
    report = report_path.read_text(encoding="utf-8")
    rows = [
        {
            "project_id": "0e3a7d4f826e72153e2256f9ec7cc250",
            "title": "杂多县昂赛乡三江源雪豹小镇清洁能源智慧供暖提升改造项目 采购方式 竞争性磋商",
            "region": "青海省",
        },
        {
            "project_id": "5ab8411fec7adb4ea27a547f275ec1fc",
            "title": "宁夏星海新能源有限责任公司石嘴山市100万千瓦光伏发电复合项目接入系统设计技术服务",
            "region": "宁夏回族自治区",
        },
        {
            "project_id": "8e3c4f8f182240ad7537fcbebc2fed50",
            "title": "南疆兵团第一师天盈石化绿电直连项目（EPC总承包）",
            "region": "新疆生产建设兵团",
        },
        {
            "project_id": "1dbd00544670ebda317ef75c69b723f7",
            "title": "渭南光明电力集团有限公司2026年千村万户“光伏+”乡村振兴示范项目物资采购竞争谈判公告",
            "region": "陕西省",
        },
    ]
    payload = run_recall_benchmark(
        benchmark_path=APP_ROOT / "config/recall_benchmarks/known_cases.yaml",
        project_rows=rows,
        artifact_texts={"output/sessions/search_20260831_180208_a5d128ac/search_report.md": report},
        root=APP_ROOT.parent,
    )
    assert payload["total_cases"] == 11
    assert payload["unavailable_cases"] == 3
    assert payload["matched_cases"] >= 8
    assert payload["region_summary"]["青海省"]["matched"] == 1
    assert payload["region_summary"]["宁夏回族自治区"]["matched"] == 1
    assert payload["region_summary"]["新疆生产建设兵团"]["matched"] == 1
