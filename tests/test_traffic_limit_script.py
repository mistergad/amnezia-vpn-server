from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "awg2-traffic-limit.sh"


def test_root_qdisc_is_recreated_when_its_kind_changes() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'tc qdisc replace dev "$interface" root' not in script
    assert script.count('tc qdisc del dev "$interface" root') == 2
    assert script.count('tc qdisc add dev "$interface" root handle 1: htb') == 2


def test_default_htb_class_is_created_idempotently() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    root_class = 'tc class replace dev "$interface" parent 1: classid 1:1'
    legacy_root_class = 'tc class add dev "$interface" parent 1: classid 1:1'
    assert script.count(root_class) == 2
    assert legacy_root_class not in script


def test_tc_mutations_are_serialized() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "acquire_lock()" in script
    assert "trap release_lock EXIT" in script
    assert "acquire_lock\n" in script
