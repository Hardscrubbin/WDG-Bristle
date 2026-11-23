#include "Queue.h"

#include <LittleFS.h>
#include <algorithm>

#include "BristleProto.h"
#include "Clock.h"
#include "Store.h"

static const char* QDIR = "/q";
static const char* STATE_PATH = "/q/state";
static const char SEG_MAGIC[4] = {'B', 'Q', '2', '\n'};
static const uint32_t SEG_MAGIC_LEN = 4;
static const size_t HDR = 18;
static const uint32_t STATE_MAGIC = 0x32535142;   // "BQS2"
static const uint8_t F_REL = 1;

static File rdFile;

static void put32(uint8_t* p, uint32_t v) {
  p[0] = (uint8_t)v;
  p[1] = (uint8_t)(v >> 8);
  p[2] = (uint8_t)(v >> 16);
  p[3] = (uint8_t)(v >> 24);
}
static uint32_t get32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void packHdr(const FindQueue::Rec& r, uint8_t len, uint8_t* h) {
  put32(h, r.seq);
  h[4] = r.rel ? F_REL : 0;
  h[5] = len;
  h[6] = (uint8_t)r.boot;
  h[7] = (uint8_t)(r.boot >> 8);
  put32(h + 8, r.sec);
  h[12] = (uint8_t)r.ms;
  h[13] = (uint8_t)(r.ms >> 8);
  put32(h + 14, r.session);
}

static uint8_t unpackHdr(const uint8_t* h, FindQueue::Rec& r) {
  r.seq = get32(h);
  r.rel = (h[4] & F_REL) != 0;
  r.boot = (uint16_t)(h[6] | (h[7] << 8));
  r.sec = get32(h + 8);
  r.ms = (uint16_t)(h[12] | (h[13] << 8));
  r.session = get32(h + 14);
  return h[5];
}

static bool hexField(const String& s, int from, int to, uint32_t& out) {
  out = 0;
  for (int i = from; i < to; i++) {
    char c = s[i];
    int v = (c >= '0' && c <= '9') ? c - '0' : (c >= 'a' && c <= 'f') ? c - 'a' + 10 : -1;
    if (v < 0) return false;
    out = (out << 4) | (uint32_t)v;
  }
  return true;
}

// "0000abcd.seg" (being written) or "0000abcd-01f3.seg" (finished, 0x1f3 records).
static bool parseSegName(const String& name, uint32_t& start, uint16_t& count, bool& sealed) {
  uint32_t n = 0;
  if (name.length() == 12 && name.endsWith(".seg") && hexField(name, 0, 8, start)) {
    count = 0;
    sealed = false;
    return true;
  }
  if (name.length() == 17 && name.endsWith(".seg") && name[8] == '-' && hexField(name, 0, 8, start) &&
      hexField(name, 9, 13, n)) {
    count = (uint16_t)n;
    sealed = true;
    return true;
  }
  return false;
}

String FindQueue::segPath(const Seg& s) const {
  char buf[32];
  if (s.sealed) snprintf(buf, sizeof(buf), "/q/%08lx-%04x.seg", (unsigned long)s.start, (unsigned)s.count);
  else snprintf(buf, sizeof(buf), "/q/%08lx.seg", (unsigned long)s.start);
  return String(buf);
}

int FindQueue::indexOf(uint32_t start) const {
  for (size_t i = 0; i < segs_.size(); i++)
    if (segs_[i].start == start) return (int)i;
  return -1;
}

// Complete records in a segment file. -1 if it is not a segment file.
int FindQueue::scanCount(const Seg& s) {
  File f = LittleFS.open(segPath(s), "r");
  if (!f) return -1;
  uint8_t m[4];
  if (f.read(m, 4) != 4 || memcmp(m, SEG_MAGIC, 4) != 0) {
    f.close();
    return -1;
  }
  uint32_t size = f.size(), pos = SEG_MAGIC_LEN;
  int n = 0;
  uint8_t h[HDR];
  while (pos + HDR <= size) {
    if (f.read(h, HDR) != HDR) break;
    uint8_t len = h[5];
    if (pos + HDR + len > size) break;   // cut off mid-record
    pos += HDR + len;
    n++;
    f.seek(pos);
  }
  f.close();
  return n;
}

void FindQueue::loadState() {
  File f = LittleFS.open(STATE_PATH, "r");
  if (!f) return;
  uint8_t b[16];
  if (f.read(b, sizeof(b)) == sizeof(b) && get32(b) == STATE_MAGIC) {
    ackTarget_ = get32(b + 4);
    dropped_ = get32(b + 8);
    untimed_ = get32(b + 12);
  }
  f.close();
}

// The ack point moves all the time while a core is listening, and it only saves work after a
// restart (without it the node would offer a few acked finds again, which the core drops). So it is
// written at most every QUEUE_STATE_SAVE_MS. A drop counter is never left unsaved that long.
void FindQueue::saveStateIfDue() {
  if (!stateDirty_) return;
  if (countersDirty_ || millis() - lastStateMs_ >= bp::QUEUE_STATE_SAVE_MS) saveState();
}

void FindQueue::saveState() {
  stateDirty_ = countersDirty_ = false;
  lastStateMs_ = millis();
  stateSaves_++;
  if (!flashOk_) return;
  File f = LittleFS.open(STATE_PATH, "w");
  if (!f) return;
  uint8_t b[16];
  put32(b, STATE_MAGIC);
  put32(b + 4, ack_);
  put32(b + 8, dropped_);
  put32(b + 12, untimed_);
  f.write(b, sizeof(b));
  f.close();
}

bool FindQueue::begin() {
  next_ = store::reserveSeqBlock(bp::QUEUE_SEQ_BLOCK);
  reservedEnd_ = next_ + bp::QUEUE_SEQ_BLOCK;
  flashEnd_ = next_;
  ack_ = ackTarget_ = 0;
  headPos_ = SEG_MAGIC_LEN;
  headDone_ = 0;
  flashOk_ = LittleFS.begin(true);   // formats on first use
  if (!flashOk_) {
    ack_ = ackTarget_ = next_;
    return false;
  }
  if (!LittleFS.exists(QDIR)) LittleFS.mkdir(QDIR);
  totalBytes_ = LittleFS.totalBytes();
  loadState();
  if (ackTarget_ > next_) ackTarget_ = next_;

  std::vector<String> junk;
  File dir = LittleFS.open(QDIR);
  if (dir) {
    for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
      String name = f.name();
      int slash = name.lastIndexOf('/');
      if (slash >= 0) name = name.substring(slash + 1);
      f.close();
      Seg s;
      if (parseSegName(name, s.start, s.count, s.sealed)) segs_.push_back(s);
      else if (name != "state") junk.push_back(name);
    }
    dir.close();
  }
  for (const String& n : junk) LittleFS.remove(String(QDIR) + "/" + n);
  std::sort(segs_.begin(), segs_.end(), [](const Seg& a, const Seg& b) { return a.start < b.start; });

  // A run that was cut off can leave a segment without its count: count it now. Finish every
  // segment except the last one, which keeps taking new finds.
  for (size_t i = 0; i < segs_.size();) {
    Seg& s = segs_[i];
    if (!s.sealed) {
      int n = scanCount(s);
      if (n <= 0) {
        LittleFS.remove(segPath(s));
        segs_.erase(segs_.begin() + i);
        continue;
      }
      s.count = (uint16_t)n;
      if (i + 1 < segs_.size()) seal(s);
    }
    i++;
  }
  held_ = 0;
  for (const Seg& s : segs_) held_ += s.count;
  if (!segs_.empty()) {
    if (segs_.back().sealed) {
      forceRotate_ = true;
    } else {
      File last = LittleFS.open(segPath(segs_.back()), "r");
      curSegBytes_ = last ? last.size() : 0;
      if (last) last.close();
    }
  }
  usedBytes_ = LittleFS.usedBytes();
  advancePending_ = true;   // skip what was acked before the reboot, and anything that can't be timed
  advance(700);
  return true;
}

uint32_t FindQueue::push(bool rel, uint32_t sec, uint16_t ms, uint32_t session, const String& body) {
  if (next_ >= reservedEnd_) {
    next_ = store::reserveSeqBlock(bp::QUEUE_SEQ_BLOCK);
    reservedEnd_ = next_ + bp::QUEUE_SEQ_BLOCK;
  }
  Rec r;
  r.seq = next_++;
  r.rel = rel;
  r.boot = clk::bootId();
  r.sec = sec;
  r.ms = ms;
  r.session = session;
  r.body = body;
  ram_.push_back(r);
  ramBytes_ += HDR + body.length();
  held_++;
  // (not more than twice a second, so a flash that refuses writes can't slow every find down)
  if (flashOk_ && ramBytes_ >= bp::QUEUE_RAM_FLUSH_BYTES && millis() - lastFlushMs_ >= 500) flush();
  // Flash missing or refusing writes: RAM holds what it can, then the oldest goes (counted).
  while (ramBytes_ > bp::QUEUE_RAM_MAX_BYTES && ram_.size() > 1) dropRamFront();
  return r.seq;
}

void FindQueue::dropRamFront() {
  ramBytes_ -= std::min(ramBytes_, HDR + ram_.front().body.length());
  ram_.erase(ram_.begin());
  if (held_) held_--;
  dropped_++;
  stateDirty_ = countersDirty_ = true;
  advancePending_ = true;
}

void FindQueue::tick(uint32_t nowMs) {
  if (advancePending_) advance(64);
  if (nowMs - lastFlushMs_ >= bp::QUEUE_SAVE_MS) flush();
}

void FindQueue::seal(Seg& s) {
  String from = segPath(s);
  if (rdOpen_ && rdSeg_ == s.start) closeReaderFile();   // the reader reopens it by its new name
  s.sealed = true;
  if (!LittleFS.rename(from, segPath(s))) s.sealed = false;
}

void FindQueue::rotate() {
  if (!segs_.empty() && !segs_.back().sealed) {
    if (segs_.back().count == 0) {             // never got a record: don't keep an empty file
      if (rdSeg_ == segs_.back().start) {
        closeReaderFile();
        rdValid_ = false;
      }
      LittleFS.remove(segPath(segs_.back()));
      segs_.pop_back();
    } else {
      seal(segs_.back());
    }
  }
  Seg s;
  s.start = ram_.front().seq;
  s.count = 0;
  s.sealed = false;
  segs_.push_back(s);
  curSegBytes_ = 0;
  forceRotate_ = false;
  if (segs_.size() == 1) {
    headPos_ = SEG_MAGIC_LEN;
    headDone_ = 0;
  }
}

// Removes the oldest segment file. Whatever it still held is gone: the caller counts it.
void FindQueue::removeHead() {
  if (segs_.empty()) return;
  if (rdSeg_ == segs_.front().start) {
    closeReaderFile();
    rdValid_ = false;
  }
  LittleFS.remove(segPath(segs_.front()));
  segs_.erase(segs_.begin());
  headPos_ = SEG_MAGIC_LEN;
  headDone_ = 0;
  if (segs_.empty()) curSegBytes_ = 0;
}

// Flash nearly full: the oldest finds go, and are counted.
void FindQueue::makeRoom() {
  usedBytes_ = LittleFS.usedBytes();
  while (segs_.size() > 1 && totalBytes_ < usedBytes_ + bp::QUEUE_MIN_FREE_BYTES) {
    uint32_t lost = segs_.front().count > headDone_ ? segs_.front().count - headDone_ : 0;
    removeHead();
    dropped_ += lost;
    held_ -= std::min(held_, lost);
    if (ack_ < segs_.front().start) ack_ = segs_.front().start;
    if (ackTarget_ < ack_) ackTarget_ = ack_;
    advancePending_ = true;
    stateDirty_ = countersDirty_ = true;
    usedBytes_ = LittleFS.usedBytes();
  }
}

void FindQueue::flush() {
  lastFlushMs_ = millis();
  if (!flashOk_) return;
  if (ram_.empty()) {
    usedBytes_ = LittleFS.usedBytes();
    saveStateIfDue();
    return;
  }
  flushes_++;
  makeRoom();
  if (segs_.empty() || forceRotate_ || curSegBytes_ >= bp::QUEUE_SEGMENT_BYTES) rotate();
  Seg& seg = segs_.back();
  String path = segPath(seg);
  File f = LittleFS.open(path, "a");
  if (!f) return;   // keep it in RAM, try again next time
  uint32_t wrote = 0;
  if (curSegBytes_ == 0) {
    f.write((const uint8_t*)SEG_MAGIC, SEG_MAGIC_LEN);
    wrote += SEG_MAGIC_LEN;
  }
  for (const Rec& r : ram_) {
    uint8_t len = (uint8_t)std::min<size_t>(r.body.length(), 255);
    uint8_t h[HDR];
    packHdr(r, len, h);
    f.write(h, HDR);
    f.write((const uint8_t*)r.body.c_str(), len);
    wrote += HDR + len;
  }
  f.close();

  // LittleFS only reports a full flash when the file is closed, and close() can't tell us. So look.
  File chk = LittleFS.open(path, "r");
  uint32_t size = chk ? chk.size() : 0;
  if (chk) chk.close();
  flashedBytes_ += wrote;
  if (size == curSegBytes_ + wrote) {
    seg.count = (uint16_t)(seg.count + ram_.size());
    curSegBytes_ += wrote;
    flashEnd_ = ram_.back().seq + 1;
    ram_.clear();
    ramBytes_ = 0;
    saveState();
    return;
  }

  // Not all of it landed. Count what really is in the file, keep the rest in RAM, and stop adding
  // to this file (its tail may be half a record).
  int n = scanCount(seg);
  size_t landed = n > (int)seg.count ? (size_t)(n - seg.count) : 0;
  if (landed > ram_.size()) landed = ram_.size();
  if (landed) {
    flashEnd_ = ram_[landed - 1].seq + 1;
    for (size_t i = 0; i < landed; i++) ramBytes_ -= std::min(ramBytes_, HDR + ram_[i].body.length());
    ram_.erase(ram_.begin(), ram_.begin() + landed);
  }
  seg.count = n > 0 ? (uint16_t)n : 0;
  forceRotate_ = true;
}

// Moves the ack point forward: past what the core acked, past finds that can never get a time
// (counted as dropped), and past gaps. Stops at the oldest find that still has to be delivered.
// Works in small steps so a long skip never stalls the loop.
void FindQueue::advance(int budget) {
  File f;
  bool open = false;
  auto finish = [&]() {
    if (ackTarget_ < ack_) ackTarget_ = ack_;
    advancePending_ = false;
    if (open) f.close();
  };
  while (budget-- > 0) {
    if (flashOk_ && !segs_.empty()) {
      Seg& head = segs_.front();
      if (!open) {
        String path = segPath(head);
        f = LittleFS.open(path, "r");
        open = (bool)f;
        if (!open && LittleFS.exists(path)) return;   // can't read it right now: try again next tick
        if (open) f.seek(headPos_);
      }
      Rec r;
      uint8_t h[HDR], len = 0;
      bool got = open && f.read(h, HDR) == HDR;
      if (got) {
        len = unpackHdr(h, r);
        got = (uint32_t)f.position() + len <= (uint32_t)f.size();
      }
      if (!got) {
        // End of this segment (or a damaged tail): nothing more can come out of it.
        if (open) f.close();
        open = false;
        uint32_t lost = head.count > headDone_ ? head.count - headDone_ : 0;
        removeHead();
        if (lost) {
          dropped_ += lost;
          held_ -= std::min(held_, lost);
          countersDirty_ = true;
        }
        stateDirty_ = true;
        continue;
      }
      bool acked = r.seq < ackTarget_;
      bool dead = !acked && !clk::canResolve(r.rel, r.boot);
      if (!acked && !dead) {
        ack_ = r.seq;
        finish();
        return;
      }
      headPos_ += HDR + len;
      headDone_++;
      f.seek(headPos_);
      if (held_) held_--;
      if (dead) {
        dropped_++;
        untimed_++;
        countersDirty_ = true;
      }
      ack_ = r.seq + 1;
      stateDirty_ = true;
      continue;
    }
    if (!ram_.empty() && ram_.front().seq < ackTarget_) {
      ack_ = ram_.front().seq + 1;
      ramBytes_ -= std::min(ramBytes_, HDR + ram_.front().body.length());
      ram_.erase(ram_.begin());
      if (held_) held_--;
      stateDirty_ = true;
      continue;
    }
    ack_ = ram_.empty() ? next_ : ram_.front().seq;
    finish();
    return;
  }
  if (open) f.close();
}

void FindQueue::ack(uint32_t q) {
  if (q > next_) q = next_;
  if (q <= ackTarget_) return;
  ackTarget_ = q;
  advancePending_ = true;
  advance(400);
}

void FindQueue::closeReaderFile() {
  if (rdOpen_) rdFile.close();
  rdOpen_ = false;
}

bool FindQueue::peek(uint32_t from, Rec& out) {
  if (from < ack_) from = ack_;
  if (flashOk_ && !segs_.empty() && from < flashEnd_) {
    int idx = (rdValid_ && from == rdLastSeq_ + 1) ? indexOf(rdSeg_) : -1;
    if (idx < 0) {   // not where we left off: find the segment that holds `from`
      closeReaderFile();
      idx = 0;
      for (size_t i = 0; i < segs_.size(); i++)
        if (segs_[i].start <= from) idx = (int)i;
      rdSeg_ = segs_[idx].start;
      rdPos_ = SEG_MAGIC_LEN;
      rdValid_ = false;
    }
    while (idx < (int)segs_.size()) {
      if (!rdOpen_) {
        rdFile = LittleFS.open(segPath(segs_[idx]), "r");
        rdOpen_ = (bool)rdFile;
        if (rdOpen_) rdFile.seek(rdPos_);
      }
      uint8_t h[HDR];
      while (rdOpen_ && rdFile.read(h, HDR) == HDR) {
        Rec r;
        uint8_t len = unpackHdr(h, r);
        char buf[256];
        if (rdFile.read((uint8_t*)buf, len) != len) break;
        rdPos_ += HDR + len;
        if (r.seq >= from) {
          buf[len] = 0;
          r.body = buf;
          out = r;
          rdLastSeq_ = r.seq;
          rdValid_ = true;
          return true;
        }
      }
      // Nothing more in this segment for now. A file we have open does not see later appends, so
      // close it; the next call reopens it at the same place.
      closeReaderFile();
      if (idx + 1 >= (int)segs_.size()) break;
      idx++;
      rdSeg_ = segs_[idx].start;
      rdPos_ = SEG_MAGIC_LEN;
    }
  }
  for (const Rec& r : ram_) {
    if (r.seq >= from) {
      out = r;
      return true;
    }
  }
  return false;
}

void FindQueue::clear() {
  closeReaderFile();
  rdValid_ = false;
  if (flashOk_)
    for (const Seg& s : segs_) LittleFS.remove(segPath(s));
  segs_.clear();
  ram_.clear();
  ramBytes_ = 0;
  curSegBytes_ = 0;
  headPos_ = SEG_MAGIC_LEN;
  headDone_ = 0;
  held_ = dropped_ = untimed_ = 0;
  ack_ = ackTarget_ = flashEnd_ = next_;
  advancePending_ = forceRotate_ = false;
  if (flashOk_) {
    saveState();
    usedBytes_ = LittleFS.usedBytes();
  }
}

uint8_t FindQueue::fillPct() const {
  return totalBytes_ ? (uint8_t)(((uint64_t)usedBytes_ * 100) / totalBytes_) : 0;
}
