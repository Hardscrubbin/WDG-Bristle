// Find queue: RAM buffer + LittleFS segments. Survives reboots. A find leaves only when the core acks
// it, when flash is full (oldest first, counted), or when it can never get a time (counted).
//
// Sequence numbers are never reused: they are reserved in NVS in blocks, and a reboot starts a fresh
// block (so there are gaps; nothing ever sits in a gap).
//
// On flash: /q/<first seq, 8 hex>.seg is the segment being written, /q/<first seq>-<records, 4 hex>.seg
// is a finished one. A segment is "BQ2\n" followed by records:
//   u32 seq | u8 flags | u8 len | u16 boot | u32 sec | u16 ms | u32 session | len bytes of body
// flags bit 0 (REL): sec/ms are the uptime of boot `boot`, not unix time (see Clock.h).
// body is the find without its q/t/ms/s fields, e.g. "AP:b=aabbccddeeff,c=6,r=-61,k=3,e=Home".
// /q/state holds the ack point and the drop counters.
//
// ackSeq() is always the sequence number of the oldest find still held (nextSeq() when there are
// none). The core's ACK moves it; so do drops. It never stops inside a gap.
#pragma once
#include <Arduino.h>
#include <vector>

class FindQueue {
 public:
  struct Rec {
    uint32_t seq = 0;
    bool rel = false;
    uint16_t boot = 0;
    uint32_t sec = 0;
    uint16_t ms = 0;
    uint32_t session = 0;
    String body;
  };

  bool begin();                                       // false: no flash, the queue is RAM only
  bool flashOk() const { return flashOk_; }
  // Queue a find captured now. Returns its sequence number.
  uint32_t push(bool rel, uint32_t sec, uint16_t ms, uint32_t session, const String& body);
  void tick(uint32_t nowMs);                          // periodic save + background tidy-up
  void flush();                                       // write RAM to flash now
  void ack(uint32_t q);                               // the core holds every find with seq < q
  bool peek(uint32_t from, Rec& out);                 // first held find with seq >= from
  void clear();                                       // everything, counters too (node was freed)

  uint32_t nextSeq() const { return next_; }
  uint32_t ackSeq() const { return ack_; }
  uint32_t backlog() const { return held_; }          // finds held right now, exactly
  uint32_t dropped() const { return dropped_; }       // lost for good since the node was adopted
  uint32_t untimed() const { return untimed_; }       // the part of dropped() that had no clock
  uint32_t capacityBytes() const { return totalBytes_; }
  uint32_t usedBytes() const { return usedBytes_; }
  uint8_t fillPct() const;
  size_t segments() const { return segs_.size(); }
  // Flash wear, since this boot: how often finds were written, how many bytes, how often the small
  // state file (ack point, drop counters) was written.
  uint32_t flushes() const { return flushes_; }
  uint32_t flashedBytes() const { return flashedBytes_; }
  uint32_t stateSaves() const { return stateSaves_; }

 private:
  struct Seg {
    uint32_t start;      // seq of its first record
    uint16_t count;      // records in the file
    bool sealed;         // finished: the count is in the file name
  };
  String segPath(const Seg& s) const;
  int scanCount(const Seg& s);                        // -1: not a segment file
  void seal(Seg& s);
  void rotate();
  void makeRoom();
  void removeHead();
  void advance(int budget);
  void dropRamFront();
  void closeReaderFile();
  void loadState();
  void saveState();
  void saveStateIfDue();
  int indexOf(uint32_t start) const;

  bool flashOk_ = false;
  std::vector<Seg> segs_;                             // ascending
  std::vector<Rec> ram_;                              // newer than everything on flash
  size_t ramBytes_ = 0;
  uint32_t next_ = 0, reservedEnd_ = 0;
  uint32_t ack_ = 0, ackTarget_ = 0;
  uint32_t held_ = 0, dropped_ = 0, untimed_ = 0;
  uint32_t flashEnd_ = 0;                             // every record on flash has seq < flashEnd_
  uint32_t curSegBytes_ = 0;
  uint32_t headPos_ = 0;                              // file offset of the first held record in segs_[0]
  uint16_t headDone_ = 0;                             // records of segs_[0] before headPos_
  bool advancePending_ = false, stateDirty_ = false, forceRotate_ = false;
  bool countersDirty_ = false;                        // a drop was counted: save the state at the next chance
  uint32_t lastFlushMs_ = 0, lastStateMs_ = 0;
  uint32_t flushes_ = 0, flashedBytes_ = 0, stateSaves_ = 0;
  uint32_t totalBytes_ = 0, usedBytes_ = 0;
  // delivery reader
  bool rdOpen_ = false, rdValid_ = false;
  uint32_t rdSeg_ = 0, rdPos_ = 0, rdLastSeq_ = 0;
};
