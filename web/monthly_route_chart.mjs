/** Build a chart model from observed monthly rows without filling time gaps. */
export function buildMonthlyRouteChartModel(records) {
  if (!Array.isArray(records)) throw new TypeError("Monthly route records must be an array");

  const points = records.map((record) => {
    const match = /^(\d{4})-(\d{2})-01$/.exec(String(record.month_start ?? ""));
    const amount = Number(record.boardings);
    if (!match || Number(match[2]) < 1 || Number(match[2]) > 12 || !Number.isFinite(amount) || amount < 0) {
      throw new TypeError("Monthly route history contains an invalid observed record");
    }
    return {
      month_start: record.month_start,
      route_name: String(record.route_name ?? ""),
      boardings: amount,
      month_index: Number(match[1]) * 12 + Number(match[2]) - 1,
    };
  }).sort((left, right) => left.month_index - right.month_index);

  const segments = [];
  for (const point of points) {
    const current = segments.at(-1);
    if (!current || point.month_index !== current.at(-1).month_index + 1) segments.push([point]);
    else current.push(point);
  }

  return {
    points,
    segments,
    first_month_index: points[0]?.month_index ?? null,
    last_month_index: points.at(-1)?.month_index ?? null,
    max_boardings: Math.max(0, ...points.map((point) => point.boardings)),
  };
}
