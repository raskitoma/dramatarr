from app.services.synopsis import clean_synopsis


def test_lark_synopsis_keeps_plot_and_drops_character_appendix() -> None:
    raw = (
        '<div data-lark-html-role="root">'
        "<div>Regres&oacute; muchos a&ntilde;os despu&eacute;s.</div><div>&nbsp;</div>"
        "<div>Domina la <strong>magia</strong> de drag&oacute;n.</div>"
        "<div>Personajes:</div><div>Rian Calder</div></div>"
    )
    assert clean_synopsis(raw) == "Regresó muchos años después.\n\nDomina la magia de dragón."


def test_preserves_plain_text_and_removes_non_content_markup() -> None:
    assert clean_synopsis("One\n\nTwo & three.") == "One\n\nTwo & three."
    assert clean_synopsis("<script>hidden()</script><p>Visible</p>") == "Visible"
    assert clean_synopsis("<div>&nbsp;</div>") is None
