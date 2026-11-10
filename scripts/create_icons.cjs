const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function chunk(name, data) {
  const type = Buffer.from(name, "ascii");
  const body = Buffer.concat([type, data]);
  const output = Buffer.alloc(12 + data.length);
  output.writeUInt32BE(data.length, 0);
  type.copy(output, 4);
  data.copy(output, 8);
  output.writeUInt32BE(crc32(body), 8 + data.length);
  return output;
}
function render(size) {
  const pixels = Buffer.alloc(size * size * 4);
  const rect = (x1, y1, x2, y2, color) => {
    for (let y = Math.max(0, y1); y < Math.min(size, y2); y++) {
      for (let x = Math.max(0, x1); x < Math.min(size, x2); x++) {
        const offset = (y * size + x) * 4;
        pixels[offset] = color[0]; pixels[offset + 1] = color[1]; pixels[offset + 2] = color[2]; pixels[offset + 3] = 255;
      }
    }
  };
  const circle = (cx, cy, radius, color) => {
    for (let y = Math.max(0, cy - radius); y < Math.min(size, cy + radius); y++) {
      for (let x = Math.max(0, cx - radius); x < Math.min(size, cx + radius); x++) {
        if ((x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2) {
          const offset = (y * size + x) * 4;
          pixels[offset] = color[0]; pixels[offset + 1] = color[1]; pixels[offset + 2] = color[2]; pixels[offset + 3] = 255;
        }
      }
    }
  };
  const s = size / 128;
  const box = (x1, y1, x2, y2, color) => rect(Math.round(x1 * s), Math.round(y1 * s), Math.round(x2 * s), Math.round(y2 * s), color);
  const dot = (cx, cy, radius, color) => circle(Math.round(cx * s), Math.round(cy * s), Math.round(radius * s), color);
  const navy = [16, 26, 44], bus = [71, 210, 205], glass = [24, 43, 70], light = [182, 242, 238], white = [244, 251, 255], wheel = [215, 228, 242];
  rect(0, 0, size, size, navy);
  box(28, 28, 100, 90, bus); box(34, 20, 94, 78, bus);
  box(37, 31, 91, 68, glass); box(43, 38, 62, 61, light); box(68, 38, 85, 61, light);
  box(36, 74, 92, 84, white); dot(45, 94, 8, wheel); dot(83, 94, 8, wheel);
  dot(28, 47, 3, white); dot(100, 47, 3, white);
  const rowBytes = size * 4;
  const scanlines = Buffer.alloc(size * (rowBytes + 1));
  for (let y = 0; y < size; y++) pixels.copy(scanlines, y * (rowBytes + 1) + 1, y * rowBytes, (y + 1) * rowBytes);
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0); header.writeUInt32BE(size, 4); header[8] = 8; header[9] = 6;
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]), chunk("IHDR", header), chunk("IDAT", zlib.deflateSync(scanlines)), chunk("IEND", Buffer.alloc(0))]);
}
const target = path.join(__dirname, "..", "web");
for (const size of [192, 512]) fs.writeFileSync(path.join(target, `icon-${size}.png`), render(size));
console.log("Generated Android install icons (192 and 512 px)");
