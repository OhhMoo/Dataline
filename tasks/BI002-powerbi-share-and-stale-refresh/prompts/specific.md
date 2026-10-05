# ANA-242 - Executive Overview: region shares don't add up, and February is short

From: Hanna Kowalski (Analytics engineering lead)
To: whoever is on call

Leadership's Executive Overview in Power BI is wrong in two ways James found.
This is the state I need things in when you finish. It is a floor, not a
walkthrough.

1. **Segment Share is its definition, published.** What the Power BI service
   serves for the `Commerce` dataset computes Segment Share as
   `docs/metrics.md` defines it - Net Revenue with the segment filter
   removed - so filtered to any region, each segment's share is of that
   region and a region's segments add up to 100%. Every other tile shows
   exactly what it shows now on the same data. Check it by querying the
   published dataset, not your local files.

2. **The dashboard shows the warehouse as it is now.** The published dataset
   is an import model; it holds what its last refresh loaded. After you are
   done it holds the warehouse as it stands - February's Net Revenue on the
   dashboard is the warehouse's, to the cent.

3. **The fix is the definition, and the repo is the source of truth.** The
   measure is fixed in the TMDL in `bi/powerbi`, committed, and the
   definition the service runs is exactly what is committed (deployed the
   way `ae/runbooks/bi-release-process` says). No numbers or region names
   typed into a measure, no filters added to tiles, no measure removed.

4. **Nothing else moves.** Nothing in the warehouse, the dbt models,
   `models/semantic` or `docs/metrics.md` changes.

5. **James hears it from us.** On ANA-242: the right February Net Revenue,
   what was wrong with Segment Share, and that the dataset was refreshed.

---

When you are done, write `/workspace/final_report.json`:

```json
{
  "summary": "<what was wrong and what you did, in a few sentences>",
  "root_cause": "<what was behind each of the two problems - the mechanism, not the symptom>",
  "february_net_revenue": "<February 2026's Net Revenue as the dashboard should show it, as a number>",
  "deployed": ["<each BI tool whose published definition you changed: powerbi>"],
  "refreshed": ["<each Power BI dataset you refreshed: Commerce>"],
  "changes": ["<each file you changed, and why>"]
}
```
