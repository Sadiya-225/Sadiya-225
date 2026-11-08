// Generates GitHub stats SVG cards locally (no rate-limited public instance).
// Usage: node generate-cards.mjs <path-to-github-readme-stats-checkout> <outDir>
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [, , statsDir, outDir] = process.argv;
const load = (p) => import(pathToFileURL(path.resolve(statsDir, p)).href);
const { fetchStats } = await load("src/fetchers/stats.js");
const { fetchTopLanguages } = await load("src/fetchers/top-languages.js");
const { renderStatsCard } = await load("src/cards/stats.js");
const { renderTopLanguages } = await load("src/cards/top-languages.js");

const style = {
  theme: "react",
  bg_color: "1F222E",
  title_color: "F85D7F",
  icon_color: "F8D866",
  hide_border: true,
};

fs.mkdirSync(outDir, { recursive: true });

// Stats card: original account (holds the contribution history, incl. private)
const stats = await fetchStats("Sadiya-125", true);
fs.writeFileSync(
  path.join(outDir, "stats.svg"),
  renderStatsCard(stats, { ...style, show_icons: true }),
);

// Top languages: public repos of the mirror account
const langs = await fetchTopLanguages("Sadiya-225", [], 1, 1);
fs.writeFileSync(
  path.join(outDir, "top-langs.svg"),
  renderTopLanguages(langs, { ...style, layout: "compact", langs_count: 8 }),
);
console.log("cards written to", outDir);

// Contribution graph: last 60 days of daily contributions as a self-contained SVG
const token = process.env.PAT_1;
const gql = await fetch("https://api.github.com/graphql", {
  method: "POST",
  headers: { Authorization: `bearer ${token}`, "Content-Type": "application/json" },
  body: JSON.stringify({
    query: `{ user(login:"Sadiya-125"){ contributionsCollection{ contributionCalendar{ weeks{ contributionDays{ date contributionCount } } } } } }`,
  }),
}).then((r) => r.json());
const days = gql.data.user.contributionsCollection.contributionCalendar.weeks
  .flatMap((w) => w.contributionDays)
  .slice(-60);
const W = 900, H = 300, L = 50, R = 20, T = 55, B = 45;
const max = Math.max(5, ...days.map((d) => d.contributionCount));
const x = (i) => L + (i * (W - L - R)) / (days.length - 1);
const y = (v) => T + (1 - v / max) * (H - T - B);
const pts = days.map((d, i) => `${x(i).toFixed(1)},${y(d.contributionCount).toFixed(1)}`);
const grid = [0, 0.25, 0.5, 0.75, 1]
  .map((f) => {
    const v = Math.round(max * f), yy = y(v);
    return `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" stroke="#F85D7F" stroke-opacity=".15"/><text x="${L - 8}" y="${yy + 4}" fill="#9aa0b5" font-size="11" text-anchor="end">${v}</text>`;
  })
  .join("");
const labels = days
  .map((d, i) => (i % 10 === 0 ? `<text x="${x(i)}" y="${H - 18}" fill="#9aa0b5" font-size="11" text-anchor="middle">${d.date.slice(5)}</text>` : ""))
  .join("");
const dots = days.map((d, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(d.contributionCount).toFixed(1)}" r="3" fill="#F8D866"><title>${d.date}: ${d.contributionCount}</title></circle>`).join("");
fs.writeFileSync(
  path.join(outDir, "activity-graph.svg"),
  `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" font-family="Segoe UI, Ubuntu, sans-serif"><rect width="${W}" height="${H}" rx="8" fill="#1F222E"/><text x="${L}" y="32" fill="#F85D7F" font-size="18" font-weight="600">Contribution Activity (last 60 days)</text>${grid}<polygon points="${x(0)},${y(0)} ${pts.join(" ")} ${x(days.length - 1)},${y(0)}" fill="#F85D7F" fill-opacity=".25"/><polyline points="${pts.join(" ")}" fill="none" stroke="#F85D7F" stroke-width="2.5" stroke-linejoin="round"/>${dots}${labels}</svg>`,
);
