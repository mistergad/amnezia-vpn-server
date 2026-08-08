from pathlib import Path


def test_default_htb_class_is_created_idempotently() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "awg2-traffic-limit.sh"
    ).read_text(encoding="utf-8")

    root_class = 'tc class replace dev "$interface" parent 1: classid 1:1'
    legacy_root_class = 'tc class add dev "$interface" parent 1: classid 1:1'
    assert script.count(root_class) == 2
    assert legacy_root_class not in script
