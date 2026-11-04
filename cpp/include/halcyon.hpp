// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Halcyon contributors
// Halcyon modem, C++ port -- wide profile (2.75 kHz, 117 carriers).
//
// This is the same air interface as halcyon/ in Python: the same OFDM geometry,
// the same preamble and pilot signs, the same LDPC parity-check matrices, the
// same interleaver, the same frame header and the same QAM mapping.  The tables
// that are expensive or RNG-dependent in Python are shipped as data (see
// tools/dump_cpp_data.py); everything that is work -- FFT, equalisation, QAM,
// belief propagation, framing -- is implemented here.
//
// The profiles are deliberately limited to "wide" for now: the robust and
// compact geometries are the same code with different constants, but porting
// them without their own measured thresholds would be pretending they work.
#pragma once

#include <complex>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace halcyon {

using cpx = std::complex<double>;

// One decoded (or partially decoded) frame.
struct Result {
  bool header_ok = false;
  bool ok = false;  // header plus every payload block
  std::string payload;
  int mcs = -1;
  int blocks = 0;
  int ftype = -1;
  int seq = -1;
  int ack_bitmap = 0;
  int mcs_sugg = -1;
  double snr_reported = -99.0;
  double snr_db = -99.0;
  double cfo_hz = 0.0;
  int sample_start = -1;
  int header_iters = 0;
  bool sync_ok = false;
  std::vector<bool> blocks_ok;
};

// A frame as the transmitter produced it, for tests and for the CLI.
struct Built {
  std::vector<cpx> wave;
  int blocks = 0;
  int mcs = -1;
  double airtime_s = 0.0;
};

class Modem {
 public:
  // data_path defaults to "cpp/data/wide.bin" relative to the working dir.
  explicit Modem(const std::string& data_path = "");

  // Modulate `text` at rung `mcs`.  Throws std::runtime_error if the payload
  // does not fit (12 blocks) or the rung does not exist.
  Built build(const std::string& text, int mcs, int seq = 0) const;

  // Demodulate and decode.  Never throws on a bad capture: an unsynchronised
  // or undecodable input returns a Result with ok == false.
  Result receive(const std::vector<cpx>& rx) const;

  int rung_count() const;

  // Rung metadata, for a CLI listing.
  struct RungInfo {
    std::string name;
    int bps = 0;
    int n = 0;
    int k = 0;
    int rep = 1;
    int block_mult = 1;
    bool uncoded = false;
    double max_bytes = 0.0;
  };
  RungInfo rung(int idx) const;

  double sample_rate() const;

 private:
  struct Impl;
  std::shared_ptr<Impl> impl_;
};

// Complex float I/O: interleaved little-endian float32 (re, im), the format
// both this CLI and the Python dumper use.
std::vector<cpx> read_iq(const std::string& path);
void write_iq(const std::string& path, const std::vector<cpx>& wave);

}  // namespace halcyon
