// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Halcyon contributors
// Halcyon modem core, C++.
//
// The air interface is defined by the tables in halcyon_tables.hpp / wide.bin
// (generated from the Python implementation): geometry, preamble, pilot signs,
// parity-check matrices and interleaver.  The algorithms below reproduce the
// Python ones so that a frame from either end decodes at the other.

#include "halcyon.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <limits>
#include <stdexcept>

#include "halcyon_tables.hpp"

namespace halcyon {
namespace {

constexpr double kPi = 3.14159265358979323846;

// The header field widths sum to 60, not the 59 that halcyon/frame.py claims in
// a comment: 2+2+5+6+16+8+12+5+4.  Python uses sum(widths), so 60 is the wire
// truth and the CRC covers the first 60 bits.
constexpr int kHeaderPayloadBits = 60;
constexpr int kHeaderInfoBits = 76;

bool debug_on() {
  static const bool on = std::getenv("HALCYON_DEBUG") != nullptr;
  return on;
}

// ---------------------------------------------------------------- small maths

void fft(std::vector<cpx>& a, bool inverse) {
  const int n = static_cast<int>(a.size());
  if (n < 2) return;
  for (int i = 1, j = 0; i < n; ++i) {
    int bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) std::swap(a[i], a[j]);
  }
  for (int len = 2; len <= n; len <<= 1) {
    const double ang = 2.0 * kPi / len * (inverse ? 1.0 : -1.0);
    const cpx wl(std::cos(ang), std::sin(ang));
    for (int i = 0; i < n; i += len) {
      cpx w(1.0, 0.0);
      for (int j = 0; j < len / 2; ++j) {
        const cpx u = a[i + j];
        const cpx v = a[i + j + len / 2] * w;
        a[i + j] = u + v;
        a[i + j + len / 2] = u - v;
        w *= wl;
      }
    }
  }
  // Unnormalised on purpose: numpy's ifft is scaled by 1/N and Phy._symbol
  // multiplies by sqrt(N) straight after, which cancels to this.
}

int next_pow2(int n) {
  int p = 1;
  while (p < n) p <<= 1;
  return p;
}

double clampd(double v, double lo, double hi) { return std::min(hi, std::max(lo, v)); }

// ---------------------------------------------------------------- CRC-16/CCITT

uint16_t crc16_of(const std::vector<uint8_t>& bits) {
  static uint16_t table[256];
  static bool ready = false;
  if (!ready) {
    for (int i = 0; i < 256; ++i) {
      uint16_t c = static_cast<uint16_t>(i << 8);
      for (int k = 0; k < 8; ++k) {
        c = (c & 0x8000) ? static_cast<uint16_t>((c << 1) ^ 0x1021)
                         : static_cast<uint16_t>(c << 1);
      }
      table[i] = c;
    }
    ready = true;
  }
  std::vector<uint8_t> data = bits;
  while (data.size() % 8) data.push_back(0);
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < data.size(); i += 8) {
    uint8_t byte = 0;
    for (int k = 0; k < 8; ++k) byte = static_cast<uint8_t>((byte << 1) | data[i + k]);
    crc = static_cast<uint16_t>((crc << 8) ^ table[((crc >> 8) ^ byte) & 0xFF]);
  }
  return crc;
}

std::vector<uint8_t> crc16_bits(const std::vector<uint8_t>& bits) {
  const uint16_t v = crc16_of(bits);
  std::vector<uint8_t> out(16);
  for (int i = 0; i < 16; ++i) out[i] = static_cast<uint8_t>((v >> (15 - i)) & 1);
  return out;
}

std::vector<uint8_t> bytes_to_bits(const std::string& s) {
  std::vector<uint8_t> out;
  out.reserve(s.size() * 8);
  for (unsigned char c : s) {
    for (int i = 7; i >= 0; --i) out.push_back(static_cast<uint8_t>((c >> i) & 1));
  }
  return out;
}

std::string bits_to_bytes(const std::vector<uint8_t>& bits) {
  std::vector<uint8_t> b = bits;
  while (b.size() % 8) b.push_back(0);
  std::string out;
  out.reserve(b.size() / 8);
  for (size_t i = 0; i < b.size(); i += 8) {
    uint8_t byte = 0;
    for (int k = 0; k < 8; ++k) byte = static_cast<uint8_t>((byte << 1) | b[i + k]);
    out.push_back(static_cast<char>(byte));
  }
  return out;
}

// --------------------------------------------------------------------- LDPC

struct Code {
  int n = 0;
  int k = 0;
  int rows = 0;
  std::vector<int> edge_row, edge_col;
  std::vector<int> row_ptr, row_edges;
  std::vector<int> col_ptr, col_edges;

  void build(int n_, int k_, const std::vector<int>& pairs) {
    n = n_;
    k = k_;
    rows = n - k;
    const int e = static_cast<int>(pairs.size() / 2);
    edge_row.resize(e);
    edge_col.resize(e);
    for (int i = 0; i < e; ++i) {
      edge_row[i] = pairs[2 * i];
      edge_col[i] = pairs[2 * i + 1];
    }
    row_ptr.assign(rows + 1, 0);
    for (int i = 0; i < e; ++i) row_ptr[edge_row[i] + 1]++;
    for (int r = 0; r < rows; ++r) row_ptr[r + 1] += row_ptr[r];
    row_edges.resize(e);
    {
      std::vector<int> fill(row_ptr.begin(), row_ptr.end() - 1);
      for (int i = 0; i < e; ++i) row_edges[fill[edge_row[i]]++] = i;
    }
    col_ptr.assign(n + 1, 0);
    for (int i = 0; i < e; ++i) col_ptr[edge_col[i] + 1]++;
    for (int c = 0; c < n; ++c) col_ptr[c + 1] += col_ptr[c];
    col_edges.resize(e);
    {
      std::vector<int> fill(col_ptr.begin(), col_ptr.end() - 1);
      for (int i = 0; i < e; ++i) col_edges[fill[edge_col[i]]++] = i;
    }
  }

  // H = [A | D] with D the staircase that makes it invertible; so parity is a
  // suffix XOR of the information syndromes, exactly as in Python.
  std::vector<uint8_t> encode(const std::vector<uint8_t>& info) const {
    std::vector<uint8_t> s(rows, 0);
    for (int c = 0; c < k; ++c) {
      if (!info[c]) continue;
      for (int p = col_ptr[c]; p < col_ptr[c + 1]; ++p) {
        const int e = col_edges[p];
        s[edge_row[e]] ^= 1;
      }
    }
    std::vector<uint8_t> cw(n, 0);
    for (int c = 0; c < k; ++c) cw[c] = info[c];
    uint8_t acc = 0;
    for (int r = rows - 1; r >= 0; --r) {
      acc ^= s[r];
      cw[k + r] = acc;
    }
    return cw;
  }

  bool syndrome_ok(const std::vector<uint8_t>& bits) const {
    for (int r = 0; r < rows; ++r) {
      int par = 0;
      for (int p = row_ptr[r]; p < row_ptr[r + 1]; ++p) par ^= bits[edge_col[row_edges[p]]];
      if (par) return false;
    }
    return true;
  }

  // Log-domain sum-product, tanh rule, with a syndrome early exit.
  bool decode(const std::vector<double>& llr_in, int max_iter, std::vector<uint8_t>& bits,
              int& iters_used) const {
    const int e = static_cast<int>(llr_in.size());
    if (e < n) {
      bits.assign(n, 0);
      iters_used = 0;
      return false;
    }
    const int ne = static_cast<int>(edge_row.size());
    std::vector<double> msg(ne, 0.0);
    std::vector<double> post(llr_in.begin(), llr_in.begin() + n);
    std::vector<double> v2c(ne);
    std::vector<double> logabs(ne);
    bits.assign(n, 0);
    for (int it = 1; it <= max_iter; ++it) {
      for (int i = 0; i < ne; ++i) v2c[i] = post[edge_col[i]] - msg[i];
      for (int r = 0; r < rows; ++r) {
        const int b = row_ptr[r];
        const int en = row_ptr[r + 1];
        double sum = 0.0;
        int negs = 0;
        for (int p = b; p < en; ++p) {
          const int i = row_edges[p];
          const double v = clampd(v2c[i], -60.0, 60.0);
          double t = std::tanh(v * 0.5);
          if (std::fabs(t) < 1e-12) t = (t < 0 ? -1e-12 : 1e-12);
          const double la = std::log(std::min(1.0, std::fabs(t)));
          logabs[i] = la;
          sum += la;
          if (v < 0) ++negs;
        }
        for (int p = b; p < en; ++p) {
          const int i = row_edges[p];
          double excl = sum - logabs[i];
          const int neg = (v2c[i] < 0) ? 1 : 0;
          const double sign = ((negs - neg) & 1) ? -1.0 : 1.0;
          const double mag = std::exp(std::min(excl, 0.0));
          msg[i] = sign * 2.0 * std::atanh(std::min(mag, 1.0 - 1e-12));
        }
      }
      post.assign(llr_in.begin(), llr_in.begin() + n);
      for (int i = 0; i < ne; ++i) post[edge_col[i]] += msg[i];
      for (int c = 0; c < n; ++c) bits[c] = post[c] < 0.0 ? 1 : 0;
      iters_used = it;
      if (syndrome_ok(bits)) return true;
    }
    return false;
  }
};

// ---------------------------------------------------------------------- QAM

struct Qam {
  int bps = 2;
  int half = 1;
  int nlev = 2;
  std::vector<double> lv;

  explicit Qam(int bits_per_symbol) : bps(bits_per_symbol) {
    if (bps == 1) {
      lv = {1.0, -1.0};
      half = 0;
      nlev = 2;
      return;
    }
    half = bps / 2;
    nlev = 1 << half;
    const double scale = std::sqrt(2.0 * (static_cast<double>(nlev) * nlev - 1.0) / 3.0);
    lv.resize(nlev);
    for (int i = 0; i < nlev; ++i) {
      const int g = i ^ (i >> 1);
      lv[i] = (2.0 * g - (nlev - 1)) / scale;
    }
  }

  cpx map(const uint8_t* bits) const {
    if (bps == 1) return bits[0] == 0 ? cpx(1.0, 0.0) : cpx(-1.0, 0.0);
    int ii = 0, qi = 0;
    for (int j = 0; j < half; ++j) ii = (ii << 1) | bits[j];
    for (int j = 0; j < half; ++j) qi = (qi << 1) | bits[half + j];
    return cpx(lv[ii], lv[qi]);
  }

  void demap(const cpx& s, double var, double* out) const {
    const double v = std::max(var, 1e-9);
    if (bps == 1) {
      out[0] = 4.0 * s.real() / v;
      return;
    }
    for (int j = 0; j < half; ++j) {
      double d0i = 1e300, d1i = 1e300, d0q = 1e300, d1q = 1e300;
      for (int i = 0; i < nlev; ++i) {
        const double dr = s.real() - lv[i];
        const double di = s.imag() - lv[i];
        const double a = dr * dr;
        const double b = di * di;
        if ((i >> (half - 1 - j)) & 1) {
          d1i = std::min(d1i, a);
          d1q = std::min(d1q, b);
        } else {
          d0i = std::min(d0i, a);
          d0q = std::min(d0q, b);
        }
      }
      out[j] = (d1i - d0i) / v;
      out[half + j] = (d1q - d0q) / v;
    }
  }
};

// ------------------------------------------------------------------- helpers

double interp_at(const std::vector<int>& xs, const std::vector<double>& ys, int x) {
  if (x <= xs.front()) return ys.front();
  if (x >= xs.back()) return ys.back();
  size_t i = 1;
  while (i < xs.size() && xs[i] < x) ++i;
  const double x0 = xs[i - 1], x1 = xs[i];
  const double t = (x - x0) / (x1 - x0);
  return ys[i - 1] + t * (ys[i] - ys[i - 1]);
}

struct RungMeta {
  std::string name;
  int index = 0;
  int bps = 0;
  int rep = 1;
  int block_mult = 1;
  bool uncoded = false;
  int n = 0;
  int k = 0;
  int edges = 0;
  size_t edge_off = 0;
  size_t perm_off[13] = {0};
};

}  // namespace

// ----------------------------------------------------------------------- Impl

struct Modem::Impl {
  std::vector<char> blob;
  std::vector<cpx> preamble;
  std::vector<cpx> pilot_syms;
  std::vector<int> pilot_bins;
  std::vector<int> data_bins;
  Code header;
  std::vector<Code> codes;      // one per rung (may be shared)
  std::vector<bool> has_code;
  std::vector<RungMeta> rungs;
  std::vector<std::vector<int>> perms;  // [rung][blocks-1]

  const float* f32(size_t off) const {
    return reinterpret_cast<const float*>(blob.data() + off);
  }
  const int32_t* i32(size_t off) const {
    return reinterpret_cast<const int32_t*>(blob.data() + off);
  }

  void load(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) throw std::runtime_error("cannot open " + path +
                                      " (pass --data, set HALCYON_DATA, or run tools/dump_cpp_data.py)");
    blob.assign(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
    if (blob.size() < 1024) throw std::runtime_error("table file is truncated: " + path);

    preamble.resize(kPreambleSamples);
    for (int i = 0; i < kPreambleSamples; ++i) {
      preamble[i] = cpx(f32(kPreambleOff)[2 * i], f32(kPreambleOff)[2 * i + 1]);
    }
    pilot_syms.resize(kPilots);
    for (int i = 0; i < kPilots; ++i) {
      pilot_syms[i] = cpx(f32(kPilotOff)[2 * i], f32(kPilotOff)[2 * i + 1]);
    }
    for (int b = kBinLo; b <= kBinHi; ++b) {
      if ((b - kBinLo) % kPilotStep == 0) {
        pilot_bins.push_back(b);
      } else {
        data_bins.push_back(b);
      }
    }
    if (static_cast<int>(pilot_bins.size()) != kPilots ||
        static_cast<int>(data_bins.size()) != kDataCarriers) {
      throw std::runtime_error("table geometry does not match the header constants");
    }

    std::vector<int> hpairs(kHeaderEdges * 2);
    for (int i = 0; i < kHeaderEdges * 2; ++i) hpairs[i] = i32(kHeaderEdgeOff)[i];
    header.build(kHeaderN, kHeaderK, hpairs);

    rungs.resize(kRungCount);
    codes.resize(kRungCount);
    has_code.assign(kRungCount, false);
    perms.resize(kRungCount);
    for (int r = 0; r < kRungCount; ++r) {
      const Rung& src = kRungTable[r];
      RungMeta m;
      m.name = src.name;
      m.index = src.index;
      m.bps = src.bps;
      m.rep = src.rep;
      m.block_mult = src.block_mult;
      m.uncoded = src.uncoded != 0;
      m.n = src.n;
      m.k = src.k;
      m.edges = src.edges;
      m.edge_off = src.edge_off;
      for (int b = 0; b < 13; ++b) m.perm_off[b] = src.perm_off[b];
      rungs[r] = m;
      if (!m.uncoded) {
        std::vector<int> pairs(m.edges * 2);
        for (int i = 0; i < m.edges * 2; ++i) pairs[i] = i32(m.edge_off)[i];
        // block_code() uses the code of length n/rep: the repetition is in the
        // airtime, not in the codeword.
        codes[r].build(m.n / std::max(1, m.rep), m.k, pairs);
        has_code[r] = true;
      }

    }
  }

  std::vector<int> perm_for(int rung, int blocks) const {
    const RungMeta& m = rungs[rung];
    const size_t len = static_cast<size_t>(blocks) * m.n;
    std::vector<int> out(len);
    const int32_t* p = i32(m.perm_off[blocks]);
    for (size_t i = 0; i < len; ++i) out[i] = p[i];
    // A table that is not a permutation of [0, len) would write outside the
    // LLR buffer rather than fail; check it here so a bad dump is loud.
    int pmax = -1;
    for (int v : out) pmax = std::max(pmax, v);
    if (pmax < 0 || static_cast<size_t>(pmax) >= len) {
      throw std::runtime_error("interleaver table inconsistent for rung " +
                               std::to_string(rung) + " with " + std::to_string(blocks) +
                               " blocks (max index " + std::to_string(pmax) + ", length " +
                               std::to_string(len) + ")");
    }
    return out;
  }

  std::vector<cpx> modulate(const std::vector<cpx>& data, int n_syms) const {
    std::vector<cpx> out(static_cast<size_t>(n_syms) * kSymLen);
    std::vector<cpx> grid(kFft);
    for (int s = 0; s < n_syms; ++s) {
      std::fill(grid.begin(), grid.end(), cpx(0.0, 0.0));
      for (int p = 0; p < kPilots; ++p) grid[pilot_bins[p]] = pilot_syms[p];
      for (int d = 0; d < kDataCarriers; ++d) {
        grid[data_bins[d]] = data[static_cast<size_t>(s) * kDataCarriers + d];
      }
      fft(grid, true);
      // numpy's ifft is scaled by 1/N and Phy._symbol multiplies by sqrt(N), so
      // the net factor is 1/sqrt(N).  Get it wrong and the waveform is still
      // decodable (the receiver normalises by its channel estimate) but it is
      // not the same signal, which would break sample-level interop.
      const double scale = 1.0 / std::sqrt(static_cast<double>(kFft));
      // cyclic prefix: last cp samples, then the symbol
      for (int i = 0; i < kCp; ++i) {
        out[static_cast<size_t>(s) * kSymLen + i] = grid[kFft - kCp + i] * scale;
      }
      for (int i = 0; i < kFft; ++i) {
        out[static_cast<size_t>(s) * kSymLen + kCp + i] = grid[i] * scale;
      }
    }
    return out;
  }

  struct Demod {
    std::vector<cpx> X;
    double var = 1.0;
    double snr_db = -99.0;
  };

  Demod demodulate(const std::vector<cpx>& rx, int n_syms) const {
    Demod d;
    d.X.assign(static_cast<size_t>(n_syms) * kDataCarriers, cpx(0.0, 0.0));
    std::vector<cpx> Y(static_cast<size_t>(n_syms) * kFft, cpx(0.0, 0.0));
    std::vector<cpx> buf(kFft);
    for (int s = 0; s < n_syms; ++s) {
      const size_t base = static_cast<size_t>(s) * kSymLen + kCp;
      if (base + kFft > rx.size()) break;
      for (int i = 0; i < kFft; ++i) buf[i] = rx[base + i];
      fft(buf, false);
      for (int i = 0; i < kFft; ++i) Y[static_cast<size_t>(s) * kFft + i] = buf[i];
    }

    // H from the pilots, smoothed across pilots then (if there are enough
    // symbols) across time, exactly as the Python receiver does.
    std::vector<cpx> Hp(static_cast<size_t>(n_syms) * kPilots);
    for (int s = 0; s < n_syms; ++s) {
      for (int p = 0; p < kPilots; ++p) {
        Hp[static_cast<size_t>(s) * kPilots + p] =
            Y[static_cast<size_t>(s) * kFft + pilot_bins[p]] / pilot_syms[p];
      }
    }
    std::vector<cpx> Hs = Hp;
    for (int s = 0; s < n_syms; ++s) {
      for (int p = 0; p < kPilots; ++p) {
        const cpx a = Hp[static_cast<size_t>(s) * kPilots + std::max(0, p - 1)];
        const cpx b = Hp[static_cast<size_t>(s) * kPilots + p];
        const cpx c = Hp[static_cast<size_t>(s) * kPilots + std::min(kPilots - 1, p + 1)];
        Hs[static_cast<size_t>(s) * kPilots + p] = 0.25 * a + 0.5 * b + 0.25 * c;
      }
    }
    if (n_syms >= 3) {
      std::vector<cpx> T = Hs;
      for (int s = 0; s < n_syms; ++s) {
        const int s0 = std::max(0, s - 1), s2 = std::min(n_syms - 1, s + 1);
        for (int p = 0; p < kPilots; ++p) {
          Hs[static_cast<size_t>(s) * kPilots + p] =
              0.25 * T[static_cast<size_t>(s0) * kPilots + p] +
              0.5 * T[static_cast<size_t>(s) * kPilots + p] +
              0.25 * T[static_cast<size_t>(s2) * kPilots + p];
        }
      }
    }

    for (int s = 0; s < n_syms; ++s) {
      std::vector<double> re(kPilots), im(kPilots);
      for (int p = 0; p < kPilots; ++p) {
        re[p] = Hs[static_cast<size_t>(s) * kPilots + p].real();
        im[p] = Hs[static_cast<size_t>(s) * kPilots + p].imag();
      }
      for (int k = 0; k < kDataCarriers; ++k) {
        const int bin = data_bins[k];
        const cpx h(interp_at(pilot_bins, re, bin), interp_at(pilot_bins, im, bin));
        const double mag = std::abs(h);
        const cpx denom = mag < 1e-12 ? cpx(1e-12, 0.0) : h;
        d.X[static_cast<size_t>(s) * kDataCarriers + k] = Y[static_cast<size_t>(s) * kFft + bin] / denom;
      }
    }

    // Noise from the 3-point curvature of the pilot estimate (factor 1.5).
    double var_h = 0.0;
    double hpow = 0.0;
    int nres = 0;
    for (int s = 0; s < n_syms; ++s) {
      for (int p = 0; p < kPilots; ++p) {
        hpow += std::norm(Hp[static_cast<size_t>(s) * kPilots + p]);
        if (p >= 1 && p + 1 < kPilots) {
          const cpx res = Hp[static_cast<size_t>(s) * kPilots + p] -
                          0.5 * (Hp[static_cast<size_t>(s) * kPilots + p - 1] +
                                 Hp[static_cast<size_t>(s) * kPilots + p + 1]);
          var_h += std::norm(res);
          ++nres;
        }
      }
    }
    if (nres > 0) var_h /= (1.5 * nres);
    hpow = hpow / std::max(1, n_syms * kPilots);
    d.var = std::max(var_h / (hpow > 0.0 ? hpow : 1.0), 1e-12);
    double post = 0.0;
    for (const cpx& x : d.X) post += std::norm(x);
    post /= std::max<size_t>(1, d.X.size());
    d.snr_db = 10.0 * std::log10(std::max(post / d.var - 1.0, 1e-4));
    return d;
  }

  struct Acq {
    int start = 0;
    double cfo = 0.0;
  };

  // Best matched-filter lag of `pat` inside `sig` at carrier offset f:
  // c[k] = sum_n sig[n+k] conj(pat[n]), computed as IFFT(conj(FFT(pat)) * FFT(sig)).
  void corr_peak(const std::vector<cpx>& sig, const std::vector<cpx>& pat, double fs,
                 double f, int& idx, double& mag) const {
    const int n = next_pow2(static_cast<int>(sig.size()) + static_cast<int>(pat.size()));
    std::vector<cpx> P(n, cpx(0.0, 0.0)), Y(n, cpx(0.0, 0.0)), C(n);
    for (size_t i = 0; i < pat.size(); ++i) P[i] = pat[i];
    fft(P, false);
    for (auto& v : P) v = std::conj(v);
    const double w = -2.0 * kPi * f / fs;
    cpx ph(1.0, 0.0);
    const cpx rot(std::cos(w), std::sin(w));
    for (size_t i = 0; i < sig.size(); ++i) {
      Y[i] = sig[i] * ph;
      ph *= rot;
    }
    fft(Y, false);
    for (int i = 0; i < n; ++i) C[i] = Y[i] * P[i];
    fft(C, true);
    const int limit = static_cast<int>(sig.size()) - static_cast<int>(pat.size());
    mag = -1.0;
    idx = 0;
    for (int i = 0; i <= limit; ++i) {
      const double m = std::abs(C[i]);
      if (m > mag) {
        mag = m;
        idx = i;
      }
    }
  }

  // Matched filter against the preamble over a carrier-offset grid, coarse
  // then fine, like Phy.acquire -- but full rate, since correctness first.
  // Two stages, as Phy.acquire: a decimated full search over +-90 Hz to find
  // the burst, then a full-rate 1 Hz refinement on a short window around the
  // coarse peak.  A single full-rate pass over every offset costs 40 FFTs of
  // the whole capture and measured ~2x slower than the Python receiver.
  bool acquire(const std::vector<cpx>& rx, Acq& out) const {
    const int np = static_cast<int>(preamble.size());
    if (static_cast<int>(rx.size()) < np + 8) return false;

    constexpr int dec = 4;
    std::vector<cpx> rxd((rx.size() + dec - 1) / dec, cpx(0.0, 0.0));
    for (size_t i = 0; i < rx.size(); ++i) rxd[i / dec] += rx[i];
    for (auto& v : rxd) v /= dec;
    std::vector<cpx> pred((np + dec - 1) / dec, cpx(0.0, 0.0));
    for (int i = 0; i < np; ++i) pred[i / dec] += preamble[i];
    for (auto& v : pred) v /= dec;

    double f0 = 0.0, best = -1.0;
    int i0 = 0;
    for (double f = -90.0; f <= 90.0 + 1e-9; f += 8.0) {
      int idx = 0;
      double mag = -1.0;
      corr_peak(rxd, pred, kFs / dec, f, idx, mag);
      if (mag > best) {
        best = mag;
        f0 = f;
        i0 = idx;
      }
    }
    if (best <= 0.0) return false;

    const int start0 = i0 * dec;
    const int lo = std::max(0, start0 - 3 * kSymLen);
    const int hi = std::min(static_cast<int>(rx.size()), start0 + 4 * kSymLen);
    if (hi - lo < np) return false;
    std::vector<cpx> seg(rx.begin() + lo, rx.begin() + hi);

    double f1 = f0, best1 = -1.0;
    int i1 = 0;
    for (double f = f0 - 8.0; f <= f0 + 8.0 + 1e-9; f += 1.0) {
      int idx = 0;
      double mag = -1.0;
      corr_peak(seg, preamble, kFs, f, idx, mag);
      if (mag > best1) {
        best1 = mag;
        f1 = f;
        i1 = idx;
      }
    }
    if (best1 <= 0.0) return false;
    out.start = lo + i1;
    out.cfo = f1;
    return true;
  }

  std::vector<cpx> derotate(const std::vector<cpx>& rx, double cfo) const {
    std::vector<cpx> out(rx.size());
    const double w = -2.0 * kPi * cfo / kFs;
    for (size_t i = 0; i < rx.size(); ++i) out[i] = rx[i] * std::exp(cpx(0.0, w * i));
    return out;
  }

  std::vector<uint8_t> header_info(int ftype, int mcs_i, int blocks, int payload_bits, int seq,
                                   int ack_bitmap, int snr_code, int mcs_sugg) const {
    std::vector<uint8_t> body;
    auto put = [&](int value, int width) {
      for (int i = width - 1; i >= 0; --i) body.push_back(static_cast<uint8_t>((value >> i) & 1));
    };
    put(2, 2);              // version
    put(ftype, 2);
    put(mcs_i, 5);
    put(blocks, 6);
    put(payload_bits, 16);
    put(seq & 0xFF, 8);
    put(ack_bitmap & 0xFFF, 12);
    put(snr_code & 0x1F, 5);
    put(mcs_sugg & 0x0F, 4);
    std::vector<uint8_t> crc = crc16_bits(body);
    body.insert(body.end(), crc.begin(), crc.end());
    return body;
  }

  std::vector<uint8_t> encode_header(const std::vector<uint8_t>& info) const {
    std::vector<uint8_t> padded(header.k, 0);
    for (size_t i = 0; i < info.size() && i < padded.size(); ++i) padded[i] = info[i];
    return header.encode(padded);
  }

  int header_repeat(int rung) const {
    const RungMeta& m = rungs[rung];
    const int base = std::max(1, m.rep);
    if (kHeaderSyms >= 4) return base;
    return base * std::max(1, m.block_mult);
  }

  // bits -> QAM symbols, carrier-major within each symbol
  std::vector<cpx> bits_to_symbols(const std::vector<uint8_t>& bits, int bps, int n_syms) const {
    Qam q(bps);
    std::vector<cpx> out(static_cast<size_t>(n_syms) * kDataCarriers);
    for (int s = 0; s < n_syms; ++s) {
      for (int d = 0; d < kDataCarriers; ++d) {
        const size_t at = (static_cast<size_t>(s) * kDataCarriers + d) * bps;
        out[static_cast<size_t>(s) * kDataCarriers + d] = q.map(&bits[at]);
      }
    }
    return out;
  }

  std::vector<double> symbols_to_llr(const std::vector<cpx>& X, int bps, double var) const {
    Qam q(bps);
    std::vector<double> out(X.size() * bps);
    std::vector<double> tmp(bps);
    for (size_t i = 0; i < X.size(); ++i) {
      q.demap(X[i], var, tmp.data());
      for (int j = 0; j < bps; ++j) out[i * bps + j] = tmp[j];
    }
    return out;
  }
};

// --------------------------------------------------------------------- Modem

Modem::Modem(const std::string& data_path) : impl_(std::make_shared<Impl>()) {
  std::string path = data_path;
  if (path.empty()) path = "cpp/data/wide.bin";
  impl_->load(path);
}

int Modem::rung_count() const { return static_cast<int>(impl_->rungs.size()); }

Modem::RungInfo Modem::rung(int idx) const {
  const RungMeta& m = impl_->rungs.at(static_cast<size_t>(idx));
  RungInfo out;
  out.name = m.name;
  out.bps = m.bps;
  out.n = m.n;
  out.k = m.k;
  out.rep = m.rep;
  out.block_mult = m.block_mult;
  out.uncoded = m.uncoded;
  out.max_bytes = static_cast<double>(m.k - 16) * kMaxBlocks / 8.0;
  return out;
}

double Modem::sample_rate() const { return kFs; }

Built Modem::build(const std::string& text, int mcs_i, int seq) const {
  const Impl& im = *impl_;
  if (mcs_i < 0 || mcs_i >= static_cast<int>(im.rungs.size())) {
    throw std::runtime_error("no such rung: " + std::to_string(mcs_i));
  }
  const RungMeta& m = im.rungs[mcs_i];
  Built out;
  out.mcs = mcs_i;

  const std::vector<uint8_t> bits = bytes_to_bits(text);
  const int cap = m.k - 16;
  const int blocks = bits.empty() ? 0 : static_cast<int>((bits.size() + cap - 1) / cap);
  if (blocks > kMaxBlocks) {
    throw std::runtime_error("payload needs " + std::to_string(blocks) + " blocks, max is " +
                             std::to_string(kMaxBlocks));
  }
  out.blocks = blocks;

  std::vector<cpx> pay;
  if (blocks > 0) {
    std::vector<uint8_t> flat;
    flat.reserve(static_cast<size_t>(blocks) * m.n);
    for (int b = 0; b < blocks; ++b) {
      std::vector<uint8_t> info(m.k, 0);
      for (int i = 0; i < cap; ++i) {
        const size_t at = static_cast<size_t>(b) * cap + i;
        info[i] = at < bits.size() ? bits[at] : 0;
      }
      std::vector<uint8_t> payload_part(info.begin(), info.begin() + cap);
      const std::vector<uint8_t> crc = crc16_bits(payload_part);
      for (int i = 0; i < 16; ++i) info[cap + i] = crc[i];

      std::vector<uint8_t> cw;
      if (m.uncoded) {
        cw = info;
      } else {
        cw = im.codes[mcs_i].encode(info);
      }
      // repetition: the codeword is sent rep times and the LLRs are summed
      for (int r = 0; r < std::max(1, m.rep); ++r) {
        flat.insert(flat.end(), cw.begin(), cw.end());
      }
    }
    const std::vector<int> perm = im.perm_for(mcs_i, blocks);
    std::vector<uint8_t> interleaved(flat.size());
    for (size_t i = 0; i < flat.size(); ++i) interleaved[i] = flat[perm[i]];
    const int n_syms = blocks * kSymsPerBlock * std::max(1, m.block_mult) * std::max(1, m.rep);
    const std::vector<cpx> syms = im.bits_to_symbols(interleaved, m.bps, n_syms);
    pay = im.modulate(syms, n_syms);
  }

  const std::vector<uint8_t> hdr = im.header_info(0, mcs_i, blocks, static_cast<int>(bits.size()),
                                                  seq, 0, 0, 0);
  const std::vector<uint8_t> cw = im.encode_header(hdr);
  const std::vector<cpx> hsyms = im.bits_to_symbols(cw, 2, kHeaderSyms);
  std::vector<cpx> hdr_wave = im.modulate(hsyms, kHeaderSyms);
  const int rep = im.header_repeat(mcs_i);
  std::vector<cpx> hdr_all;
  for (int r = 0; r < rep; ++r) hdr_all.insert(hdr_all.end(), hdr_wave.begin(), hdr_wave.end());

  out.wave.reserve(im.preamble.size() + hdr_all.size() + pay.size());
  out.wave.insert(out.wave.end(), im.preamble.begin(), im.preamble.end());
  out.wave.insert(out.wave.end(), hdr_all.begin(), hdr_all.end());
  out.wave.insert(out.wave.end(), pay.begin(), pay.end());
  out.airtime_s = static_cast<double>(out.wave.size()) / kFs;
  return out;
}

Result Modem::receive(const std::vector<cpx>& rx_in) const {
  const Impl& im = *impl_;
  Result res;
  if (rx_in.empty()) return res;
  Impl::Acq acq;
  if (!im.acquire(rx_in, acq)) return res;
  res.sync_ok = true;
  res.cfo_hz = acq.cfo;
  res.sample_start = acq.start;
  if (debug_on()) {
    std::fprintf(stderr, "[acq] start=%d cfo=%.2f Hz of %zu samples\n", acq.start, acq.cfo,
                 rx_in.size());
  }
  std::vector<cpx> rx = im.derotate(rx_in, acq.cfo);

  const int h_off = acq.start + kPreambleSyms * kSymLen;

  // The sender may have repeated the header by the payload's repetition and
  // block length, which the receiver cannot know yet: try each, let the CRC
  // settle it.
  int hdr_payload_bits = 0;
  std::vector<uint8_t> body;
  bool got = false;
  for (int hr : {1, 2, 4, 8}) {
    const int span = kHeaderSyms * hr * kSymLen;
    if (h_off + span > static_cast<int>(rx.size())) {
      if (h_off + span - static_cast<int>(rx.size()) > 4 * kSymLen) break;
      rx.resize(h_off + span, cpx(0.0, 0.0));
    }
    std::vector<cpx> seg(rx.begin() + h_off, rx.begin() + h_off + span);
    const Impl::Demod d = im.demodulate(seg, kHeaderSyms * hr);
    std::vector<double> llr = im.symbols_to_llr(d.X, 2, d.var);
    if (hr > 1) {
      const size_t per = llr.size() / hr;
      std::vector<double> summed(per, 0.0);
      for (int r = 0; r < hr; ++r) {
        for (size_t i = 0; i < per; ++i) summed[i] += llr[r * per + i];
      }
      llr = summed;
    }
    std::vector<uint8_t> bits;
    int iters = 0;
    const bool conv = im.header.decode(llr, 60, bits, iters);
    if (debug_on()) {
      std::fprintf(stderr, "[hdr] hr=%d var=%.3g snr=%.1f converged=%d iters=%d\n", hr, d.var,
                   d.snr_db, static_cast<int>(conv), iters);
    }
    res.header_iters = iters;
    if (bits.size() < static_cast<size_t>(kHeaderK)) continue;
    std::vector<uint8_t> head(bits.begin(), bits.begin() + kHeaderInfoBits);
    const std::vector<uint8_t> crc = crc16_bits(
        std::vector<uint8_t>(head.begin(), head.begin() + kHeaderPayloadBits));
    bool crc_ok = true;
    for (int i = 0; i < 16; ++i) {
      if (head[kHeaderPayloadBits + i] != crc[i]) {
        crc_ok = false;
        break;
      }
    }
    if (!crc_ok) continue;
    int pos = 0;
    auto take = [&](int width) {
      int v = 0;
      for (int i = 0; i < width; ++i) v = (v << 1) | head[pos + i];
      pos += width;
      return v;
    };
    const int version = take(2);
    const int ftype = take(2);
    const int mcs_hdr = take(5);
    const int blocks = take(6);
    const int payload_bits = take(16);
    const int seq = take(8);
    const int bitmap = take(12);
    const int snr_code = take(5);
    const int sugg = take(4);
    if (version != 2 || mcs_hdr >= static_cast<int>(im.rungs.size())) continue;
    res.ftype = ftype;
    res.mcs = mcs_hdr;
    res.blocks = blocks;
    res.seq = seq;
    res.ack_bitmap = bitmap;
    res.snr_reported = static_cast<double>(snr_code) - 8.0;
    res.mcs_sugg = sugg;
    res.snr_db = d.snr_db;
    res.header_ok = true;
    hdr_payload_bits = payload_bits;
    got = true;
    break;
  }
  if (!got) return res;
  if (res.ftype != 0 || res.blocks == 0) return res;

  const RungMeta& m = im.rungs[res.mcs];
  const int rep = std::max(1, m.rep);
  const int p_off = h_off + kHeaderSyms * im.header_repeat(res.mcs) * kSymLen;
  const int n_syms = res.blocks * kSymsPerBlock * std::max(1, m.block_mult) * rep;
  const int need = p_off + n_syms * kSymLen;
  if (need > static_cast<int>(rx.size())) {
    if (need - static_cast<int>(rx.size()) > kSymLen) {
      res.blocks_ok.assign(res.blocks, false);
      return res;
    }
    rx.resize(need, cpx(0.0, 0.0));
  }
  std::vector<cpx> seg(rx.begin() + p_off, rx.begin() + need);
  const Impl::Demod d = im.demodulate(seg, n_syms);
  res.snr_db = std::isfinite(d.snr_db) ? d.snr_db : res.snr_db;
  std::vector<double> llr = im.symbols_to_llr(d.X, m.bps, d.var);

  const std::vector<int> perm = im.perm_for(res.mcs, res.blocks);
  std::vector<double> flat(llr.size());
  if (debug_on()) {
    int pmax = -1;
    for (int v : perm) pmax = std::max(pmax, v);
    std::fprintf(stderr,
                 "[pay] mcs=%d blocks=%d n=%d k=%d rep=%d n_syms=%d llr=%zu perm=%zu perm_max=%d\n",
                 res.mcs, res.blocks, m.n, m.k, rep, n_syms, llr.size(), perm.size(), pmax);
  }
  for (size_t i = 0; i < llr.size() && i < perm.size(); ++i) flat[perm[i]] = llr[i];

  const int cap = m.k - 16;
  std::vector<std::vector<uint8_t>> chunks(res.blocks);
  std::vector<bool> ok_flags(res.blocks, false);
  for (int b = 0; b < res.blocks; ++b) {
    std::vector<double> block_llr(m.n, 0.0);
    if (rep > 1) {
      const int per = m.n / rep;
      for (int r = 0; r < rep; ++r) {
        for (int i = 0; i < per; ++i) {
          block_llr[i] += flat[static_cast<size_t>(b) * m.n + static_cast<size_t>(r) * per + i];
        }
      }
    } else {
      for (int i = 0; i < m.n; ++i) {
        block_llr[i] = flat[static_cast<size_t>(b) * m.n + i];
      }
    }
    std::vector<uint8_t> bits;
    if (m.uncoded) {
      bits.assign(block_llr.begin(), block_llr.end());
      for (auto& v : bits) v = v < 0.0 ? 1 : 0;
    } else {
      int iters = 0;
      im.codes[res.mcs].decode(block_llr, 40, bits, iters);
    }
    if (static_cast<int>(bits.size()) < m.k) bits.resize(m.k, 0);
    std::vector<uint8_t> chunk(bits.begin(), bits.begin() + cap);
    const std::vector<uint8_t> crc = crc16_bits(chunk);
    bool ok = true;
    for (int i = 0; i < 16; ++i) {
      if (bits[cap + i] != crc[i]) {
        ok = false;
        break;
      }
    }
    ok_flags[b] = ok;
    if (ok) chunks[b] = chunk;
  }
  res.blocks_ok = ok_flags;
  // A partial frame is not reassembled: a hole in the middle would silently
  // shift every following bit.  The ARQ layer resends, so report it as failed.
  res.ok = std::all_of(ok_flags.begin(), ok_flags.end(), [](bool v) { return v; });
  if (res.ok) {
    std::vector<uint8_t> all_bits;
    for (const auto& c : chunks) all_bits.insert(all_bits.end(), c.begin(), c.end());
    if (hdr_payload_bits > 0 &&
        all_bits.size() > static_cast<size_t>(hdr_payload_bits)) {
      all_bits.resize(static_cast<size_t>(hdr_payload_bits));
    }
    res.payload = bits_to_bytes(all_bits);
  }
  return res;
}

// ------------------------------------------------------------------------ I/O

std::vector<cpx> read_iq(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) throw std::runtime_error("cannot open " + path);
  std::vector<float> raw;
  in.seekg(0, std::ios::end);
  const std::streamoff bytes = in.tellg();
  in.seekg(0, std::ios::beg);
  raw.resize(static_cast<size_t>(bytes) / sizeof(float));
  in.read(reinterpret_cast<char*>(raw.data()), bytes);
  std::vector<cpx> out(raw.size() / 2);
  for (size_t i = 0; i < out.size(); ++i) out[i] = cpx(raw[2 * i], raw[2 * i + 1]);
  return out;
}

void write_iq(const std::string& path, const std::vector<cpx>& wave) {
  std::ofstream out(path, std::ios::binary);
  if (!out) throw std::runtime_error("cannot write " + path);
  for (const cpx& v : wave) {
    const float pair[2] = {static_cast<float>(v.real()), static_cast<float>(v.imag())};
    out.write(reinterpret_cast<const char*>(pair), sizeof(pair));
  }
}

}  // namespace halcyon
