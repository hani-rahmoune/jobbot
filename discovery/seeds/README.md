# Seed lists for `jobbot.discover`

This directory holds plain text files of **company websites** -- one per
line, or `name,url` -- that `python -m jobbot.discover --input <file>` reads
to go find each company's own careers page and detect its ATS.

## What goes in a seed file

Company home pages or "about" pages. Never a job board URL, never a listing
page from an aggregator. Per CLAUDE.md rule 3, a directory is fine for
*finding an employer* -- you look a company up, copy its own website, paste
it here. Fetching listings *from* the directory itself is what's forbidden,
and that boundary is exactly why this step is manual: a human decides which
companies are worth adding, `jobbot.discover` only ever fetches the
employer's own domain (or a detected ATS's own API host) after that.

```
# comment lines and blank lines are ignored
https://example.fr
Acme Corp, https://acme.example
```

## Where to find companies to paste in

None of these are scraped automatically -- open them in a browser, skim the
list, and copy the company websites that look relevant (data/AI focus,
20-500 people, Paris/Nantes/wherever you're targeting this batch) into a
seed file by hand.

- **French Tech Nantes** -- <https://www.lafrenchtech-nantes.fr/> -- the
  official Nantes French Tech community's member directory.
- **Atlanpole** -- <https://www.atlanpole.fr/> -- Nantes/Loire-Atlantique's
  technology business incubator and cluster; lists resident and alumni
  startups.
- **ADN Ouest** (Alliance du Numerique Ouest) -- <https://adn-ouest.fr/> --
  digital-sector trade association covering Nantes and the wider western
  France region.
- **La French Tech national annuaire** -- <https://lafrenchtech.com/fr/> --
  the national directory; filterable by city/region and sector, useful
  beyond Nantes too.
- **Station F resident list** -- <https://stationf.co/> -- Paris' large
  startup campus; its resident/alumni directory is a dense source of small
  French tech companies, not just the well-known ones.
- **Systematic Paris-Region** -- <https://www.systematic-paris-region.org/>
  -- a deep-tech/digital innovation cluster; member directory skews data/AI
  and R&D-heavy.
- **Cap Digital** -- <https://www.capdigital.com/> -- Paris-region digital
  and creative-industries cluster; another member-directory source.

## Using a seed file

```
python -m jobbot.discover --input discovery/seeds/example.txt --delay 2.0
```

See `discovery/seeds/example.txt` for a real, verified starting list, and
the M7 milestone report for what running it actually found.
