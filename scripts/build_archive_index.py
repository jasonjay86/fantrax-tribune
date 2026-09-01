"""
build_archive_index.py — generate editions/index.html listing past editions.

Reads filenames of the form editions/week-NN.html and renders a simple page.
"""
import re
from pathlib import Path

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{{ title }} — Past Editions</title>
<link rel="stylesheet" href="../static/style.css">
</head>
<body>
<div class="paper">
  <header class="masthead">
    <div class="edition-line">Archive · Past Editions</div>
    <h1 class="title">{{ title }}</h1>
    <div class="rule rule-double"></div>
  </header>
  <section class="rankings">
    <table class="rankings-table">
      <thead>
        <tr>
          <th class="col-rank">#</th>
          <th class="col-team">Edition</th>
          <th class="col-owner">Link</th>
        </tr>
      </thead>
      <tbody>
        {% for ed in editions %}
        <tr>
          <td class="col-rank">{{ loop.index }}</td>
          <td class="col-team">{{ ed.label }}</td>
          <td class="col-owner"><a href="{{ ed.href }}">Read &rarr;</a></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </section>
  <footer class="colophon">
    <div class="rule rule-double"></div>
    <p><a href="../index.html">&larr; Return to Latest Edition</a></p>
  </footer>
</div>
</body>
</html>
"""

def main():
    root = Path("editions")
    files = []
    for f in sorted(root.glob("week-*.html")):
        m = re.match(r"week-(\d+)\.html", f.name)
        if m:
            files.append((int(m.group(1)), f.name))

    files.sort(reverse=True)  # newest first

    editions = [
        {"label": f"Week {wk:02d}", "href": fname}
        for wk, fname in files
    ]

    from jinja2 import Template
    html = Template(INDEX_TEMPLATE).render(
        title="The Keep The Change League Tribune",
        editions=editions,
    )
    out = root / "index.html"
    out.write_text(html)
    print(f"[archive] wrote {out} ({len(editions)} editions)")


if __name__ == "__main__":
    main()