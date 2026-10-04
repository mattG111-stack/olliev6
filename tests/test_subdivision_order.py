from assistant.shortlist import render_shortlist
def test_subdivision_estimate_order_before_limit():
    rows=[dict(id=i,address=str(i)+" Road",fair_value=v) for i,v in [(1,100),(2,None),(3,300),(4,200)]]
    answer=" ".join("["+r["address"]+"](/property/"+str(r["id"])+")" for r in rows)
    text=render_shortlist(answer,rows,2,"Find subdivision sites")
    assert text.index("[3 Road]")<text.index("[4 Road]")
    assert "[1 Road]" not in text and "[2 Road]" not in text
def test_regular_shortlist_keeps_order():
    rows=[dict(id=1,address="One",fair_value=100),dict(id=2,address="Two",fair_value=200)]
    text=render_shortlist("[One](/property/1) [Two](/property/2)",rows,2,"Find homes")
    assert text.index("[One]")<text.index("[Two]")
