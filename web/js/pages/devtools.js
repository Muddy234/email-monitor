/**
 * Dev Tools page — entry point.
 * Auth guard, nav, tab switching with lazy panel init.
 */
import { requireAuth, listenAuthChanges } from "../auth.js";
import { renderNav } from "../nav.js";
import { getParam, setParam } from "../ui.js";
import { initOnboarding } from "../devtools/onboarding.js";
import { initDraftTester } from "../devtools/draft-tester.js";
import { initScorerInspector } from "../devtools/scorer-inspector.js";
import { initPipelineTrace } from "../devtools/pipeline-trace.js";

await requireAuth();
listenAuthChanges();
await renderNav();

// -------------------------------------------------------------------------
// Tab switching with lazy init
// -------------------------------------------------------------------------

const PANELS = {
    onboarding: { init: initOnboarding, loaded: false },
    "draft-tester": { init: initDraftTester, loaded: false },
    scorer: { init: initScorerInspector, loaded: false },
    trace: { init: initPipelineTrace, loaded: false },
};

const tabs = document.querySelectorAll("#devtoolsTabs .em-devtools-tab");
const panels = document.querySelectorAll(".em-devtools-panel");

function activateTab(panelId) {
    tabs.forEach(t => t.classList.toggle("active", t.dataset.panel === panelId));
    panels.forEach(p => p.classList.toggle("active", p.id === `panel-${panelId}`));
    setParam("tab", panelId === "onboarding" ? "" : panelId);

    const entry = PANELS[panelId];
    if (entry && !entry.loaded) {
        entry.loaded = true;
        entry.init();
    }
}

tabs.forEach(tab => {
    tab.addEventListener("click", () => activateTab(tab.dataset.panel));
});

// Init from URL or default
const initialTab = getParam("tab", "onboarding");
activateTab(PANELS[initialTab] ? initialTab : "onboarding");
