// 11ty config. Input is the repo root so memories/ (sibling of src/) is also
// processed. Layout/include/data dirs explicitly point inside src/.
const markdownIt = require("markdown-it");

const md = new markdownIt({
  html: true,        // memory transcripts use raw <details> blocks
  linkify: true,
  typographer: false,
  breaks: false,
});

module.exports = function(eleventyConfig) {
  // ── Static assets ──────────────────────────────────────────────
  eleventyConfig.addPassthroughCopy({ "src/css": "css" });
  eleventyConfig.addPassthroughCopy({ "src/js": "js" });

  // ── Markdown engine override ───────────────────────────────────
  eleventyConfig.setLibrary("md", md);

  // ── Don't process these as templates ───────────────────────────
  eleventyConfig.ignores.add("README.md");
  eleventyConfig.ignores.add("DISASTER-RECOVERY.md");
  eleventyConfig.ignores.add("node_modules/**");
  eleventyConfig.ignores.add(".git/**");
  eleventyConfig.ignores.add(".github/**");
  eleventyConfig.ignores.add(".claude/**");
  eleventyConfig.ignores.add("scripts/**");
  eleventyConfig.ignores.add("memories/_drafts/**");

  // ── Collections ────────────────────────────────────────────────
  // Unique dates that have at least one memory (newest first)
  eleventyConfig.addCollection("memoryDates", (collection) => {
    const memories = collection.getFilteredByTag("memory");
    return [...new Set(memories.map(m => m.data.date))].sort().reverse();
  });

  // Unique YYYY-MM strings that have at least one memory (newest first)
  eleventyConfig.addCollection("memoryMonths", (collection) => {
    const memories = collection.getFilteredByTag("memory");
    return [...new Set(memories.map(m => m.data.date.slice(0, 7)))].sort().reverse();
  });

  // Map of date → array of memories on that date (for the calendar cell links)
  eleventyConfig.addCollection("memoriesByDate", (collection) => {
    const memories = collection.getFilteredByTag("memory");
    const byDate = {};
    for (const m of memories) {
      (byDate[m.data.date] = byDate[m.data.date] || []).push(m);
    }
    return byDate;
  });

  // ── Filters ────────────────────────────────────────────────────
  // ISO date → "Mon, 3 May 2026"
  eleventyConfig.addFilter("formatDate", (dateStr) => {
    if (!dateStr) return "";
    const d = new Date(dateStr + "T00:00:00Z");
    return d.toLocaleDateString("en-GB", {
      weekday: "short", year: "numeric", month: "long", day: "numeric"
    });
  });

  // YYYY-MM → "May 2026"
  eleventyConfig.addFilter("formatMonth", (yearMonth) => {
    if (!yearMonth) return "";
    const d = new Date(yearMonth + "-01T00:00:00Z");
    return d.toLocaleDateString("en-GB", { year: "numeric", month: "long" });
  });

  // Build a calendar grid for one month: returns array of cells.
  // Each cell: { blank } or { day, date, count, entries }.
  // entries are the actual memory items so templates can render hover-popovers.
  eleventyConfig.addFilter("monthGrid", (yearMonth, memoriesByDate) => {
    const [year, month] = yearMonth.split("-").map(Number);
    const firstWeekday = (new Date(Date.UTC(year, month - 1, 1)).getUTCDay() + 6) % 7; // Mon=0
    const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
    const cells = [];
    for (let i = 0; i < firstWeekday; i++) cells.push({ blank: true });
    for (let d = 1; d <= daysInMonth; d++) {
      const date = `${year}-${String(month).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
      const entries = memoriesByDate[date] || [];
      cells.push({
        day: d,
        date,
        count: entries.length,
        entries,
      });
    }
    return cells;
  });

  // GitHub-style calendar-year heatmap: 7 rows x N columns covering the
  // current year to date, aligned so each column is a Mon..Sun week.
  // Returns an array in column-major order ready for `grid-auto-flow: column`.
  // Each cell: { date, count, level (0-4), future, blank }.
  //  - blank:  a leading day that belongs to the previous year (transparent).
  //  - future: a day later this week than today (visible but empty), so the
  //            current week renders as a full column instead of leaving today
  //            as a lone square floating off the end of the grid.
  eleventyConfig.addFilter("yearHeatmap", (memoriesByDate) => {
    const today = new Date();
    today.setUTCHours(0, 0, 0, 0);
    const year = today.getUTCFullYear();
    // Anchor at Jan 1 of the current year, snapped back to the start of that
    // week (Monday). Days before Jan 1 render blank so the grid begins at Jan.
    const start = new Date(Date.UTC(year, 0, 1));
    const startDow = (start.getUTCDay() + 6) % 7; // Mon = 0
    start.setUTCDate(start.getUTCDate() - startDow);
    // Columns from the anchor week through the week that contains today.
    const dayMs = 24 * 60 * 60 * 1000;
    const weeks = Math.floor((today - start) / (7 * dayMs)) + 1;

    const cells = [];
    for (let w = 0; w < weeks; w++) {
      for (let r = 0; r < 7; r++) {
        const d = new Date(start);
        d.setUTCDate(start.getUTCDate() + w * 7 + r);
        if (d.getUTCFullYear() < year) {
          cells.push({ blank: true });   // previous-year leading days
          continue;
        }
        if (d > today) {
          cells.push({ future: true });  // rest of the current week
          continue;
        }
        const iso = d.toISOString().slice(0, 10);
        const count = (memoriesByDate[iso] || []).length;
        let level = 0;
        if (count >= 5) level = 4;
        else if (count >= 3) level = 3;
        else if (count === 2) level = 2;
        else if (count === 1) level = 1;
        cells.push({ date: iso, count, level, future: false });
      }
    }
    return cells;
  });

  // Month-label positions for the heatmap header row. Returns array of
  // { col, label } for every column where a new month starts.
  eleventyConfig.addFilter("yearHeatmapMonths", (cells) => {
    const monthAbbr = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    const labels = [];
    let lastMonth = null;
    const weeks = Math.ceil(cells.length / 7);
    for (let w = 0; w < weeks; w++) {
      for (let r = 0; r < 7; r++) {
        const c = cells[w * 7 + r];
        if (!c || c.blank || !c.date) continue;
        const m = c.date.slice(0, 7); // YYYY-MM
        if (m !== lastMonth) {
          const monthIdx = parseInt(c.date.slice(5, 7), 10) - 1;
          labels.push({ col: w + 1, label: monthAbbr[monthIdx] });
          lastMonth = m;
        }
        break;
      }
    }
    return labels;
  });

  // Filter memory list to a single date
  eleventyConfig.addFilter("onDate", (memories, date) => {
    return memories.filter(m => m.data.date === date);
  });

  // String substring (Nunjucks `slice` filter works on arrays, not strings)
  eleventyConfig.addFilter("substr", (str, start, end) => String(str).substring(start, end));

  // Compute prev/next month strings (YYYY-MM +/- 1)
  eleventyConfig.addFilter("offsetMonth", (yearMonth, delta) => {
    const [y, m] = yearMonth.split("-").map(Number);
    const d = new Date(Date.UTC(y, m - 1 + delta, 1));
    return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2,'0')}`;
  });

  return {
    dir: {
      input: ".",
      output: "_site",
      includes: "src/_includes",
      layouts: "src/_layouts",
      data: "src/_data",
    },
    markdownTemplateEngine: "njk",
    htmlTemplateEngine: "njk",
    templateFormats: ["njk", "md", "html", "11ty.js"],
  };
};
