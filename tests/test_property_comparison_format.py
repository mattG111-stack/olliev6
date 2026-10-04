from assistant.suburb_brief import SuburbBrief

def test_subdivision_comparison_keeps_sites_and_map(monkeypatch):
    import assistant.suburb_brief as m
    monkeypatch.setattr(m, "load_brief", lambda s: (_ for _ in ()).throw(AssertionError("Must not replace property results")))
    question="Find the five most profitable subdivision candidates across Auckland. Compare addresses, asking prices and profit side by side."
    brief=SuburbBrief(question)
    call=brief.wrap(lambda *args: "{}")
    for suburb in ["Glen Eden","Saint Heliers","Sunnyvale"]:
        call("get_property", {})
        brief.suburbs.append(suburb)
        brief.suburb=suburb
    answer="## Sites\n[Site](/property/123)\nProfit: unknown\n```apex-map\n{\"ids\":[123]}\n```"
    assert brief.append(answer)==answer

def test_incidental_compare_does_not_discard_answer(monkeypatch):
    import assistant.suburb_brief as m
    monkeypatch.setattr(m,"load_brief",lambda s:"Stats "+s)
    brief=SuburbBrief("Find subdivision sites. Compare their profits.")
    brief.suburb="Henderson";brief.suburbs=["Glen Eden","Henderson"]
    assert "Site analysis" in brief.append("Site analysis")
