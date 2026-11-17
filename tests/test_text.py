from lxml import html as lxml_html

from mitblogs.text import clean_inline, html_to_text


def render(markup: str) -> str:
    return html_to_text(lxml_html.fragment_fromstring(markup, create_parent='div'))


def test_blocks_become_paragraphs_and_inline_markup_is_joined():
    assert render('<p>One <b>two</b>\n  three</p><h2>Title</h2><p>four</p>') == 'One two three\n\nTitle\n\nfour'


def test_line_breaks():
    assert render('<p>a<br>b<br/><br>c</p>') == 'a\nb\n\nc'


def test_lists():
    assert render('<ul><li>a</li><li>b</li></ul>') == '- a\n- b'
    assert render('<ol start="3"><li>x</li><li>y</li></ol>') == '3. x\n4. y'


def test_skips_invisible_and_non_text_elements():
    markup = (
        '<p>kept</p><script>var x = 1;</script><style>p {}</style>'
        '<div hidden>archive notice</div><p style="display: none">gone</p>'
        '<span class="visually-hidden">Next</span><button>Previous</button>'
        '<iframe src="https://www.youtube.com/embed/x"></iframe><!-- comment -->'
    )
    assert render(markup) == 'kept'


def test_footnote_markers_replace_tooltips():
    markup = (
        '<p>the squanch!<span class="annotation-mod"><span class="annotation__text">\u2060'
        '<a class="annotation__number">01</a></span><span class="annotation">tooltip copy</span></span> next</p>'
    )
    assert render(markup) == 'the squanch![1] next'


def test_emoji_images_keep_their_character():
    assert render('<p>hi <img class="emoji" alt="😀" src="https://s.w.org/x.svg"> there</p>') == 'hi 😀 there'


def test_preformatted_text_keeps_its_spacing():
    assert render('<p>code:</p><pre>def f():\n    return 1</pre>') == 'code:\n\ndef f():\n    return 1'


def test_invisible_characters_are_removed():
    assert render('<p>zero\u200bwidth\u00a0space\ufeff</p>') == 'zerowidth space'
    assert clean_inline('  by\u00a0 Someone \u2060 ') == 'by Someone'
