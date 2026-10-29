"""Transform trees: how a value is computed, drawn as a tree."""

from isc_reporter import transform_tree


def account(attribute, source="HR"):
    return {"type": "accountAttribute", "attributes": {"sourceName": source, "attributeName": attribute}}


def reference(name, **attributes):
    return {"type": "reference", "attributes": {"id": name, **attributes}}


TRANSFORMS = {
    "ISO date": {"name": "ISO date", "type": "dateFormat",
                 "attributes": {"inputFormat": "dd/MM/yyyy", "outputFormat": "ISO8601"}},
    "Lifecycle": {"name": "Lifecycle", "type": "dateCompare", "attributes": {
        "firstDate": reference("ISO date", input=account("startDate")), "secondDate": "now",
        "operator": "LTE", "positiveCondition": "active", "negativeCondition": "prehire"}},
    "HR id": {"name": "HR id", "type": "accountAttribute",
              "attributes": {"sourceName": "HR", "attributeName": "id"}},
    "Loop": {"name": "Loop", "type": "upper", "attributes": {"input": reference("Loop")}},
    "Country": {"name": "Country", "type": "lookup", "attributes": {"table": {
        "FR": "France", "DE": "Germany", "IT": "Italy", "default": "Other"}}},
}


def tree_text(definition, style=transform_tree.BOX):
    root = transform_tree.Node("attribute")
    root.children.append(transform_tree.build(definition, TRANSFORMS))
    return transform_tree.draw(root, style).splitlines()


def test_a_tree_shows_each_step_down_to_the_data_read():
    email = {"type": "firstValid", "attributes": {"values": [
        account("mail", "AD"), {"type": "lower", "attributes": {"input": account("e-mail")}},
        {"type": "static", "attributes": {"value": "none"}}]}}
    assert tree_text(email) == [
        "attribute",
        "└─ firstValid",
        "   ├─ choice 1: AD.mail",
        "   ├─ choice 2: lower",
        "   │  └─ input: HR.e-mail",
        '   └─ choice 3: "none"']
    node = transform_tree.build(email, TRANSFORMS)
    assert [child.what for child in node.children] == [
        "reads the account attribute 'mail' of AD", "converts to lower case", "fixed value"]
    concat = {"type": "concat", "attributes": {"values": [account("first"), " ", account("last")]}}
    assert [c.text for c in transform_tree.build(concat, {}).children] == [
        "part 1: HR.first", 'part 2: " "', "part 3: HR.last"]


def test_named_transforms_are_expanded_where_they_are_used():
    lifecycle = transform_tree.build(reference("Lifecycle"), TRANSFORMS)
    assert lifecycle.what == "named transform (dateCompare): compares two dates"
    assert lifecycle.details == ("secondDate: now; operator: LTE; positiveCondition: active; "
                                 "negativeCondition: prehire")
    assert tree_text(reference("Lifecycle"))[1:] == [
        "└─ Lifecycle",
        "   └─ firstDate: ISO date",
        "      └─ input: HR.startDate"]
    iso = lifecycle.children[0]
    assert (iso.what, iso.details) == ("named transform (dateFormat): changes a date format",
                                       "inputFormat: dd/MM/yyyy; outputFormat: ISO8601")
    # a named transform that only reads a value keeps that value on its own line
    assert tree_text(reference("HR id"))[1:] == ["└─ HR id", "   └─ HR.id"]
    gone = transform_tree.build(reference("Gone"), TRANSFORMS)
    assert gone.what == "named transform, not found in the tenant or export"
    assert transform_tree.build(reference("Gone"), {}).what == "named transform"
    loop = transform_tree.build(reference("Loop"), TRANSFORMS)
    assert loop.children[0].what == "named transform, already drawn above: it calls itself"
    country = transform_tree.build(reference("Country"), TRANSFORMS)
    assert country.details == "table: FR = France, DE = Germany, IT = Italy, ... (4 in all)"


def test_calls_and_the_plain_style_for_pdf():
    assert transform_tree.calls(TRANSFORMS["Lifecycle"]) == ["ISO date"]
    assert transform_tree.calls(TRANSFORMS["ISO date"]) == []
    plain = tree_text(reference("Lifecycle"), transform_tree.PLAIN)
    assert plain[1] == "› Lifecycle" and plain[3] == " " * 8 + "› input: HR.startDate"
    assert all("─" not in line and "│" not in line for line in plain)

