"""Digest a scoring pack: per clip keep stats + signals + top-liked comments + complaint-keyword comments."""
import re, sys

path, out = sys.argv[1], sys.argv[2]
top_n = int(sys.argv[3]) if len(sys.argv) > 3 else 7
neg_n = int(sys.argv[4]) if len(sys.argv) > 4 else 6
text = open(path, encoding="utf-8").read()
parts = re.split(r"(?m)^## ", text)
NEG = re.compile(r"น้ำ|ยืด|เสียง|ข้าม|ตัด|หาย|ไม่สนุก|ขยะ|เบื่อ|ซ้ำ|มั่ว|แย่|ห่วย|ไม่ตรง|ฝืด|ช้า|พูดเยอะ|พล่าม|เฮงซวย|เสียดาย|ไม่มีซับ|ไม่พากย์|dislike|无声|沒聲|没声|無聲|卡|斷|重複|重复|垃圾|無聊|无聊|廢|水")
lines_out = []
for p in parts[1:]:
    ls = p.split("\n")
    head = ls[0]
    meta = [l for l in ls[1:6] if l.startswith(("ch=", "signals:", "short:", "finish:"))]
    com = []
    for l in ls[1:]:
        m = re.match(r"^(\s*)#(\d+(?:\.\d+)?) \[(\d+(?:\.\d+)?[Kk]?|ช่อง)\] (.*)$", l)
        if not m:
            continue
        lk = m.group(3)
        likes = 9999 if lk == "ช่อง" else (float(lk[:-1]) * 1000 if lk[-1] in "Kk" else float(lk))
        com.append((likes, bool(m.group(1)), l.strip()))
    top = sorted([c for c in com if not c[1]], key=lambda c: -c[0])[:top_n]
    topset = {c[2] for c in top}
    neg = [c for c in com if NEG.search(c[2]) and c[2] not in topset]
    neg = sorted(neg, key=lambda c: -c[0])[:neg_n]
    lines_out.append("## " + head[:110])
    lines_out += [m[:260] for m in meta]
    lines_out.append(f"(comment lines in pack: {len(com)})")
    lines_out += ["  " + c[2][:150] for c in top]
    if neg:
        lines_out.append("  -- complaint-keyword --")
        lines_out += ["  " + c[2][:150] for c in neg]
open(out, "w", encoding="utf-8").write("\n".join(lines_out))
print(len(lines_out), "lines,", len("\n".join(lines_out).encode("utf-8")), "bytes")
