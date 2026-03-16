/**
 * Lightweight SVG bar chart renderer.
 * No external dependencies — renders inline SVG.
 */

/**
 * Render a bar chart as an SVG element.
 * @param {HTMLElement} container
 * @param {{ label: string, value: number }[]} data
 * @param {{ color?: string, height?: number, barGap?: number }} opts
 */
export function renderBarChart(container, data, opts = {}) {
    const {
        color = "#2C4F7C",
        height = 160,
        barGap = 2,
    } = opts;

    if (!data || data.length === 0) {
        container.innerHTML = `<div class="em-chart-empty">No data for this period</div>`;
        return;
    }

    const maxVal = Math.max(...data.map(d => d.value), 1);
    const chartWidth = container.clientWidth || 400;
    const barWidth = Math.max(4, (chartWidth - (data.length - 1) * barGap) / data.length);
    const svgWidth = data.length * (barWidth + barGap) - barGap;
    const paddingTop = 20;
    const paddingBottom = 24;
    const chartHeight = height - paddingTop - paddingBottom;

    // Show ~5 labels evenly distributed
    const labelStep = Math.max(1, Math.ceil(data.length / 5));

    let bars = "";
    let labels = "";

    for (let i = 0; i < data.length; i++) {
        const x = i * (barWidth + barGap);
        const barH = (data[i].value / maxVal) * chartHeight;
        const y = paddingTop + chartHeight - barH;

        bars += `<rect x="${x}" y="${y}" width="${barWidth}" height="${barH}" rx="2" fill="${color}" opacity="0.85">
            <title>${data[i].label}: ${data[i].value}</title>
        </rect>`;

        if (i % labelStep === 0 || i === data.length - 1) {
            labels += `<text x="${x + barWidth / 2}" y="${height - 4}" text-anchor="middle" fill="#A8A29E" font-size="10" font-family="Inter, sans-serif">${data[i].label}</text>`;
        }
    }

    // Y-axis max label
    const yLabel = `<text x="0" y="${paddingTop - 6}" fill="#A8A29E" font-size="10" font-family="Inter, sans-serif">${maxVal}</text>`;

    container.innerHTML = `
        <svg width="100%" viewBox="0 0 ${svgWidth} ${height}" preserveAspectRatio="none" class="em-chart-svg">
            ${yLabel}
            ${bars}
            ${labels}
        </svg>
    `;
}
