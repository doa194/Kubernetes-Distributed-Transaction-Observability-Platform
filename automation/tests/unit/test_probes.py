"""Probe pods report reachability through their log output, which must parse reliably."""

from txplatform.probes import Target, build_probe_script, parse_probe_output


def test_script_checks_every_target_with_the_connect_timeout():
    script = build_probe_script([Target("a.svc", 8080), Target("b.svc", 9200)], connect_timeout=3)

    assert script.count("nc -z -w 3") == 2
    assert "a.svc 8080" in script and "b.svc 9200" in script


def test_output_parsing_ignores_noise_and_keeps_both_outcomes():
    output = "nc: bad address\nPROBE OK a.svc:8080\nsomething else\nPROBE BLOCKED b.svc:9200\n"

    assert parse_probe_output(output) == {"a.svc:8080": True, "b.svc:9200": False}
