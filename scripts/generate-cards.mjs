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
