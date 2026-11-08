// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Halcyon contributors
//
// halcyon-tui -- the station console in a terminal.
//
// The modem, radio, host service and CAT live in the Python console
// (`python -m halcyon.ui`).  This is a client of that console's loopback JSON
// API, not a second modem: it polls the same endpoints the page uses, so a
// machine with no browser -- a headless box, an SSH session -- can still work
// the station.  No third-party dependencies: POSIX sockets, termios and ANSI.

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <termios.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

// ----------------------------------------------------------------------- JSON
// The console answers small, well-formed documents.  A full parser is overkill,
// but string search is not a parser: this keeps the typed access honest.
struct Json {
  enum class Type { Null, Bool, Number, String, Array, Object };

  Type type = Type::Null;
  bool boolean = false;
  double number = 0.0;
  std::string text;
  std::vector<Json> items;
  std::vector<std::pair<std::string, Json>> fields;

  const Json* get(const std::string& key) const {
    if (type != Type::Object) return nullptr;
    for (const auto& field : fields) {
      if (field.first == key) return &field.second;
    }
    return nullptr;
  }

  const Json& at(const std::string& key) const {
    static const Json missing;
    const Json* found = get(key);
    return found == nullptr ? missing : *found;
  }

  const Json& at(std::size_t index) const {
    static const Json missing;
    return index < items.size() ? items[index] : missing;
  }

  std::string get_str(const std::string& key, const std::string& fallback = "") const {
    const Json* found = get(key);
    return found != nullptr && found->type == Type::String ? found->text : fallback;
  }

  double get_num(const std::string& key, double fallback = 0.0) const {
    const Json* found = get(key);
    return found != nullptr && found->type == Type::Number ? found->number : fallback;
  }

  bool get_bool(const std::string& key, bool fallback = false) const {
    const Json* found = get(key);
    return found != nullptr && found->type == Type::Bool ? found->boolean : fallback;
  }

  std::size_t length() const {
    return type == Type::Array ? items.size() : fields.size();
  }
};

class JsonParser {
 public:
  explicit JsonParser(const std::string& text) : text_(text) {}

  Json parse() {
    Json value = parse_value();
    skip_space();
    if (pos_ != text_.size()) fail("trailing characters");
    return value;
  }

 private:
  const std::string& text_;
  std::size_t pos_ = 0;

  [[noreturn]] void fail(const std::string& why) const {
    throw std::runtime_error("invalid JSON: " + why + " at byte " + std::to_string(pos_));
  }

  void skip_space() {
    while (pos_ < text_.size()) {
      const char c = text_[pos_];
      if (c != ' ' && c != '\n' && c != '\r' && c != '\t') break;
      ++pos_;
    }
  }

  char peek() const {
    if (pos_ >= text_.size()) fail("unexpected end");
    return text_[pos_];
  }

  bool consume(char c) {
    skip_space();
    if (pos_ < text_.size() && text_[pos_] == c) {
      ++pos_;
      return true;
    }
    return false;
  }

  void expect(char c) {
    if (!consume(c)) fail(std::string("expected '") + c + "'");
  }

  void literal(const char* word) {
    const std::size_t n = std::strlen(word);
    if (text_.compare(pos_, n, word) != 0) fail(std::string("expected ") + word);
    pos_ += n;
  }

  Json parse_value() {
    skip_space();
    switch (peek()) {
      case '{':
        return parse_object();
      case '[':
        return parse_array();
      case '"': {
        Json value;
        value.type = Json::Type::String;
        value.text = parse_string();
        return value;
      }
      case 't': {
        literal("true");
        Json value;
        value.type = Json::Type::Bool;
        value.boolean = true;
        return value;
      }
      case 'f': {
        literal("false");
        Json value;
        value.type = Json::Type::Bool;
        return value;
      }
      case 'n': {
        literal("null");
        return Json{};
      }
      default:
        return parse_number();
    }
  }

  static void append_utf8(std::string& out, unsigned code) {
    if (code <= 0x7F) {
      out.push_back(static_cast<char>(code));
    } else if (code <= 0x7FF) {
      out.push_back(static_cast<char>(0xC0 | (code >> 6)));
      out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    } else if (code <= 0xFFFF) {
      out.push_back(static_cast<char>(0xE0 | (code >> 12)));
      out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    } else {
      out.push_back(static_cast<char>(0xF0 | (code >> 18)));
      out.push_back(static_cast<char>(0x80 | ((code >> 12) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    }
  }

  unsigned parse_hex4() {
    if (pos_ + 4 > text_.size()) fail("short \\u escape");
    unsigned value = 0;
    for (int i = 0; i < 4; ++i) {
      const char c = text_[pos_++];
      value <<= 4;
      if (c >= '0' && c <= '9') {
        value |= static_cast<unsigned>(c - '0');
      } else if (c >= 'a' && c <= 'f') {
        value |= static_cast<unsigned>(c - 'a' + 10);
      } else if (c >= 'A' && c <= 'F') {
        value |= static_cast<unsigned>(c - 'A' + 10);
      } else {
        fail("bad \\u escape");
      }
    }
    return value;
  }

  std::string parse_string() {
    expect('"');
    std::string out;
    for (;;) {
      if (pos_ >= text_.size()) fail("unterminated string");
      const char c = text_[pos_++];
      if (c == '"') return out;
      if (c != '\\') {
        out.push_back(c);
        continue;
      }
      if (pos_ >= text_.size()) fail("unterminated escape");
      const char esc = text_[pos_++];
      switch (esc) {
        case '"':
        case '\\':
        case '/':
          out.push_back(esc);
          break;
        case 'b':
          out.push_back('\b');
          break;
        case 'f':
          out.push_back('\f');
          break;
        case 'n':
          out.push_back('\n');
          break;
        case 'r':
          out.push_back('\r');
          break;
        case 't':
          out.push_back('\t');
          break;
        case 'u': {
          unsigned code = parse_hex4();
          if (code >= 0xD800 && code <= 0xDBFF && pos_ + 6 <= text_.size() &&
              text_[pos_] == '\\' && text_[pos_ + 1] == 'u') {
            pos_ += 2;
            const unsigned low = parse_hex4();
            if (low >= 0xDC00 && low <= 0xDFFF) {
              code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
            }
          }
          append_utf8(out, code);
          break;
        }
        default:
          fail("unknown escape");
      }
    }
  }

  Json parse_number() {
    skip_space();
    const char* begin = text_.c_str() + pos_;
    char* end = nullptr;
    errno = 0;
    const double value = std::strtod(begin, &end);
    if (end == begin || errno == ERANGE) fail("bad number");
    pos_ += static_cast<std::size_t>(end - begin);
    Json out;
    out.type = Json::Type::Number;
    out.number = value;
    return out;
  }

  Json parse_array() {
    expect('[');
    Json out;
    out.type = Json::Type::Array;
    if (consume(']')) return out;
    for (;;) {
      out.items.push_back(parse_value());
      if (consume(']')) return out;
      expect(',');
    }
  }

  Json parse_object() {
    expect('{');
    Json out;
    out.type = Json::Type::Object;
    if (consume('}')) return out;
    for (;;) {
      skip_space();
      std::string key = parse_string();
      expect(':');
      out.fields.emplace_back(std::move(key), parse_value());
      if (consume('}')) return out;
      expect(',');
    }
  }
};

std::string json_quote(const std::string& text) {
  std::string out;
  for (unsigned char c : text) {
    switch (c) {
      case '"':
        out += "\\\"";
        break;
      case '\\':
        out += "\\\\";
        break;
      case '\n':
        out += "\\n";
        break;
      case '\r':
        out += "\\r";
        break;
      case '\t':
        out += "\\t";
        break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out.push_back(static_cast<char>(c));
        }
    }
  }
  return out;
}

std::vector<unsigned char> base64_decode(const std::string& in) {
  auto value = [](char c) -> int {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
  };
  std::vector<unsigned char> out;
  int buffer = 0;
  int bits = 0;
  for (char c : in) {
    const int v = value(c);
    if (v < 0) continue;
    buffer = (buffer << 6) | v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push_back(static_cast<unsigned char>((buffer >> bits) & 0xFF));
    }
  }
  return out;
}

// ----------------------------------------------------------------------- HTTP
struct Endpoint {
  std::string host = "127.0.0.1";
  int port = 8730;
};

struct Reply {
  int status = 0;
  std::string body;
};

Reply http_request(const Endpoint& endpoint, const std::string& method,
                   const std::string& path, const std::string& payload) {
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* addresses = nullptr;
  const std::string service = std::to_string(endpoint.port);
  const int resolved = ::getaddrinfo(endpoint.host.c_str(), service.c_str(), &hints, &addresses);
  if (resolved != 0 || addresses == nullptr) {
    throw std::runtime_error(std::string("cannot resolve ") + endpoint.host + ": " +
                             ::gai_strerror(resolved));
  }

  int fd = -1;
  std::string last_error;
  for (addrinfo* address = addresses; address != nullptr; address = address->ai_next) {
    fd = ::socket(address->ai_family, address->ai_socktype, address->ai_protocol);
    if (fd < 0) continue;
    timeval timeout{5, 0};
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    if (::connect(fd, address->ai_addr, address->ai_addrlen) == 0) break;
    last_error = std::strerror(errno);
    ::close(fd);
    fd = -1;
  }
  ::freeaddrinfo(addresses);
  if (fd < 0) {
    throw std::runtime_error("connect to " + endpoint.host + ":" + service + " failed: " +
                             (last_error.empty() ? "no route" : last_error));
  }

  std::string request = method + " " + path + " HTTP/1.1\r\n";
  request += "Host: " + endpoint.host + ":" + service + "\r\n";
  request += "User-Agent: halcyon-tui/0.1\r\n";
  request += "Accept: application/json\r\n";
  request += "Connection: close\r\n";
  if (method == "POST") {
    request += "Content-Type: application/json\r\n";
    request += "Content-Length: " + std::to_string(payload.size()) + "\r\n";
  }
  request += "\r\n";
  request += payload;

  std::size_t sent = 0;
  while (sent < request.size()) {
    const ssize_t n = ::send(fd, request.data() + sent, request.size() - sent, 0);
    if (n <= 0) {
      ::close(fd);
      throw std::runtime_error("send to the console failed");
    }
    sent += static_cast<std::size_t>(n);
  }

  std::string raw;
  char buffer[4096];
  for (;;) {
    const ssize_t n = ::recv(fd, buffer, sizeof(buffer), 0);
    if (n == 0) break;
    if (n < 0) {
      if (errno == EINTR) continue;
      ::close(fd);
      throw std::runtime_error("the console did not answer");
    }
    raw.append(buffer, static_cast<std::size_t>(n));
  }
  ::close(fd);

  const std::size_t line_end = raw.find("\r\n");
  if (line_end == std::string::npos) throw std::runtime_error("malformed HTTP reply");
  int status = 0;
  if (std::sscanf(raw.c_str(), "HTTP/%*d.%*d %d", &status) != 1) {
    throw std::runtime_error("malformed HTTP status line");
  }
  const std::size_t head_end = raw.find("\r\n\r\n");
  Reply reply;
  reply.status = status;
  reply.body = head_end == std::string::npos ? std::string() : raw.substr(head_end + 4);
  return reply;
}

Json parse_body(const Reply& reply) {
  if (reply.body.empty()) throw std::runtime_error("empty reply (HTTP " + std::to_string(reply.status) + ")");
  return JsonParser(reply.body).parse();
}

// ----------------------------------------------------------------------- text
std::string clip(const std::string& text, std::size_t width) {
  std::size_t used = 0;
  std::size_t i = 0;
  while (i < text.size() && used < width) {
    const unsigned char c = static_cast<unsigned char>(text[i]);
    std::size_t length = 1;
    if ((c & 0xE0) == 0xC0) {
      length = 2;
    } else if ((c & 0xF0) == 0xE0) {
      length = 3;
    } else if ((c & 0xF8) == 0xF0) {
      length = 4;
    }
    if (i + length > text.size()) break;
    i += length;
    ++used;
  }
  return text.substr(0, i);
}

std::vector<std::string> wrap_text(const std::string& text, std::size_t width) {
  std::vector<std::string> lines;
  if (width < 1) width = 1;
  std::string line;
  std::size_t used = 0;
  for (std::size_t i = 0; i < text.size();) {
    const unsigned char c = static_cast<unsigned char>(text[i]);
    std::size_t length = 1;
    if ((c & 0xE0) == 0xC0) {
      length = 2;
    } else if ((c & 0xF0) == 0xE0) {
      length = 3;
    } else if ((c & 0xF8) == 0xF0) {
      length = 4;
    }
    if (i + length > text.size()) length = 1;
    if (used == width) {
      lines.push_back(line);
      line.clear();
      used = 0;
    }
    line.append(text, i, length);
    i += length;
    ++used;
  }
  lines.push_back(line);
  return lines;
}

std::string mhz(double hz) {
  char buffer[40];
  std::snprintf(buffer, sizeof(buffer), "%.6f MHz", hz / 1e6);
  return buffer;
}

std::string number(double value, int precision = 1) {
  char buffer[40];
  std::snprintf(buffer, sizeof(buffer), "%.*f", precision, value);
  return buffer;
}

std::string clock_now() {
  const std::time_t now = std::time(nullptr);
  std::tm local{};
  ::localtime_r(&now, &local);
  char buffer[16];
  std::strftime(buffer, sizeof(buffer), "%H:%M:%S", &local);
  return buffer;
}

std::pair<int, int> terminal_size() {
  winsize size{};
  if (::ioctl(STDOUT_FILENO, TIOCGWINSZ, &size) == 0 && size.ws_col > 0 && size.ws_row > 0) {
    return {size.ws_col, size.ws_row};
  }
  return {100, 40};
}

// ------------------------------------------------------------------ terminal
// Raw input while drawing, cooked again while the operator types a message.
class Terminal {
 public:
  Terminal() {
    if (::isatty(STDIN_FILENO) == 0 || ::tcgetattr(STDIN_FILENO, &saved_) != 0) return;
    tty_ = true;
    enter();
  }

  ~Terminal() { leave(); }

  bool tty() const { return tty_; }
  bool raw() const { return raw_; }

  void enter() {
    if (!tty_ || raw_) return;
    termios raw = saved_;
    raw.c_lflag &= static_cast<tcflag_t>(~(ICANON | ECHO));
    raw.c_iflag &= static_cast<tcflag_t>(~(IXON | ICRNL));
    raw.c_cc[VMIN] = 0;
    raw.c_cc[VTIME] = 0;
    if (::tcsetattr(STDIN_FILENO, TCSANOW, &raw) == 0) raw_ = true;
  }

  void leave() {
    if (tty_ && raw_) {
      ::tcsetattr(STDIN_FILENO, TCSANOW, &saved_);
      raw_ = false;
    }
  }

 private:
  termios saved_{};
  bool tty_ = false;
  bool raw_ = false;
};

volatile std::sig_atomic_t g_stop = 0;

void stop_signal(int) { g_stop = 1; }

// ------------------------------------------------------------------------ app
class App {
 public:
  void set_url(const std::string& url) {
    std::string rest = url;
    if (rest.rfind("http://", 0) == 0) {
      rest = rest.substr(7);
    } else if (rest.rfind("https://", 0) == 0) {
      throw std::runtime_error("https is not supported: the console is a loopback HTTP service");
    }
    const std::size_t slash = rest.find('/');
    if (slash != std::string::npos) rest = rest.substr(0, slash);
    const std::size_t colon = rest.rfind(':');
    if (colon == std::string::npos) {
      endpoint_.host = rest;
    } else {
      endpoint_.host = rest.substr(0, colon);
      endpoint_.port = std::atoi(rest.c_str() + colon + 1);
      if (endpoint_.port <= 0 || endpoint_.port > 65535) throw std::runtime_error("bad port in --url");
    }
    if (endpoint_.host.empty()) throw std::runtime_error("empty host in --url");
    url_ = "http://" + endpoint_.host + ":" + std::to_string(endpoint_.port);
  }

  const std::string& url() const { return url_; }

  void load_modes() {
    modes_ = get("/api/modes");
    if (modes_.at("profiles").length() == 0) throw std::runtime_error("the console has no modes");
    const std::string& first = modes_.at("profiles").at(0).get_str("id");
    if (!first.empty()) profile_ = first;
    if (const char* env = std::getenv("HALCYON_PROFILE"); env != nullptr && env[0] != '\0') {
      profile_ = env;
    }
    level_ = 4;
    clamp_level();
  }

  void refresh() {
    radio_ = fetch("/api/radio", radio_, "radio");
    services_ = fetch("/api/services", services_, "host");
    cat_ = fetch("/api/cat", cat_, "CAT");
    tci_ = fetch("/api/tci", tci_, "TCI");

    if (radio_.get_bool("connected")) {
      try {
        const Json spectrum = get("/api/radio/spectrum");
        const Json* row = spectrum.get("row");
        spectrum_ = row != nullptr && row->type == Json::Type::String ? base64_decode(row->text)
                                                                      : std::vector<unsigned char>();
      } catch (const std::exception& e) {
        spectrum_.clear();
        note_error("spectrum", e.what());
      }
    } else {
      spectrum_.clear();
    }
  }

  void send(const std::string& text) {
    if (text.empty()) return;
    log("tx  " + text + "  [" + mode_tag() + "]");
    std::string body = "{\"profile\":\"" + json_quote(profile_) + "\",\"mcs\":" +
                       std::to_string(level_) + ",\"text\":\"" + json_quote(text) +
                       "\",\"snr\":" + number(snr_, 1) + ",\"channel\":\"awgn\",\"looks\":1}";
    try {
      const Json reply = post("/api/decode", body);
      const Json& attempts = reply.at("attempts");
      if (attempts.length() > 0) {
        const Json& first = attempts.at(0);
        if (first.get_bool("ok")) {
          log("rx  " + first.get_str("decoded") + "  [" +
              number(first.get_num("reported_snr"), 1) + " dB]");
        } else {
          log("rx  no copy -- header " + std::string(first.get_bool("header_ok") ? "ok" : "lost") +
              ", " + std::to_string(static_cast<int>(first.get_num("bytes"))) + " bytes");
        }
      }
      take_waterfall(reply.at("waterfall"));
      if (reply.get_bool("trimmed")) log("! message trimmed to the burst capacity");
    } catch (const std::exception& e) {
      log("! send: " + std::string(e.what()));
    }
  }

  void run_link(const std::string& text) {
    const std::string payload = text.empty() ? std::string("cq de halcyon") : text;
    log("link " + payload + "  [" + profile_ + "]");
    std::string body = "{\"profile\":\"" + json_quote(profile_) + "\",\"mcs\":" +
                       std::to_string(level_) + ",\"text\":\"" + json_quote(payload) +
                       "\",\"snr\":" + number(snr_, 1) + ",\"adaptive\":true}";
    try {
      const Json reply = post("/api/link", body);
      log("link " + std::string(reply.get_bool("complete") ? "complete" : "failed") + ": " +
          number(reply.get_num("goodput_bps"), 1) + " bps, " +
          std::to_string(static_cast<int>(reply.get_num("frames"))) + " frames, " +
          std::to_string(static_cast<int>(reply.get_num("retransmissions"))) + " retransmissions");
    } catch (const std::exception& e) {
      log("! link: " + std::string(e.what()));
    }
  }

  void discover_and_connect() {
    log("discovery: broadcasting to 255.255.255.255:1024");
    try {
      const Json found = post("/api/radio/discover", "{}");
      const Json& radios = found.at("radios");
      if (radios.length() == 0) {
        log("! no radio answered -- check power, cable and subnet");
        return;
      }
      const Json& radio = radios.at(0);
      std::string body = "{\"ip\":\"" + json_quote(radio.get_str("ip")) +
                         "\",\"rate\":48000,\"freq_hz\":14105000,\"lna_db\":16,\"drive\":8";
      for (const char* key : {"mac", "board", "firmware"}) {
        const std::string value = radio.get_str(key);
        if (!value.empty()) body += ",\"" + std::string(key) + "\":\"" + json_quote(value) + "\"";
      }
      body += "}";
      radio_ = post("/api/radio/connect", body);
      if (radio_.get_bool("connected")) {
        log("radio connected: " + radio_.get_str("board", "HL2") + " " +
            mhz(radio_.get_num("freq_hz")) + " -- receive only, keying stays on the page");
      } else {
        log("! the radio did not report a connection");
      }
    } catch (const std::exception& e) {
      log("! radio: " + std::string(e.what()));
    }
  }

  void disconnect_radio() {
    try {
      radio_ = post("/api/radio/disconnect", "{}");
      spectrum_.clear();
      log("radio disconnected");
    } catch (const std::exception& e) {
      log("! radio: " + std::string(e.what()));
    }
  }

  void toggle_host() {
    const bool running = services_.at("host").get_bool("running");
    try {
      if (running) {
        services_ = post("/api/services/stop", "{}");
        log("modem host stopped; the ports are free");
      } else {
        const char* call = std::getenv("HALCYON_MYCALL");
        const std::string mycall = call != nullptr && call[0] != '\0' ? call : "N0CALL";
        std::string body = "{\"mycall\":\"" + json_quote(mycall) +
                           "\",\"bandwidth\":2300,\"snr\":" + number(snr_, 1) +
                           ",\"channel\":\"awgn\"}";
        services_ = post("/api/services/start", body);
        log("modem host listening on 8300/8301 as " + mycall);
      }
    } catch (const std::exception& e) {
      log("! host: " + std::string(e.what()));
    }
  }

  void cycle_profile() {
    const Json& profiles = modes_.at("profiles");
    if (profiles.length() == 0) return;
    std::size_t next = 0;
    for (std::size_t i = 0; i < profiles.length(); ++i) {
      if (profiles.at(i).get_str("id") == profile_) {
        next = (i + 1) % profiles.length();
        break;
      }
    }
    profile_ = profiles.at(next).get_str("id");
    level_ = 0;
    clamp_level();
    log("profile " + profile_ + " -- " + mode_tag());
  }

  void bump_level(int delta) {
    level_ += delta;
    clamp_level();
    log("level " + mode_tag());
  }

  std::vector<std::string> render(int width, int height) const {
    width = std::max(48, width);
    height = std::max(16, height);

    std::vector<std::string> lines;
    lines.push_back(clip("Halcyon terminal   " + url_ + "   " + clock_now(), width));
    lines.push_back(clip(profile_line(), width));
    lines.push_back(clip(radio_line(), width));
    lines.push_back(clip(host_line(), width));
    lines.push_back(clip(rig_line(), width));
    lines.push_back(std::string(static_cast<std::size_t>(width), '-'));

    const int pane = 6;
    const std::vector<std::string> image = render_image(width, pane);
    if (!image.empty()) {
      lines.insert(lines.end(), image.begin(), image.end());
    } else {
      lines.push_back(clip("no radio: d discovers an HL2; m sends through the simulated channel", width));
      for (int i = 1; i < pane; ++i) lines.push_back("");
    }
    lines.push_back(std::string(static_cast<std::size_t>(width), '-'));

    const std::string footer =
        "m message  p profile  +/- level  l link  s host  d radio  x disconnect  r refresh  q quit";
    const int room = std::max(1, height - static_cast<int>(lines.size()) - 1);
    std::vector<std::string> transcript;
    for (const std::string& entry : log_) {
      const std::vector<std::string> wrapped = wrap_text(entry, static_cast<std::size_t>(width));
      transcript.insert(transcript.end(), wrapped.begin(), wrapped.end());
    }
    if (static_cast<int>(transcript.size()) > room) {
      transcript.erase(transcript.begin(), transcript.end() - room);
    }
    while (static_cast<int>(transcript.size()) < room) transcript.insert(transcript.begin(), "");
    lines.insert(lines.end(), transcript.begin(), transcript.end());
    lines.push_back(clip(footer, width));
    return lines;
  }

  void print_once(int width, int height) const {
    for (const std::string& line : render(width, height)) std::cout << line << '\n';
  }

 private:
  Endpoint endpoint_;
  std::string url_;
  Json modes_;
  Json radio_;
  Json services_;
  Json cat_;
  Json tci_;
  std::string profile_ = "wide";
  int level_ = 4;
  double snr_ = 9.0;
  std::vector<std::string> log_;
  std::vector<unsigned char> spectrum_;
  std::vector<unsigned char> waterfall_;
  int waterfall_w_ = 0;
  int waterfall_h_ = 0;
  std::vector<std::pair<std::string, std::string>> errors_;

  Json get(const std::string& path) {
    const Reply reply = http_request(endpoint_, "GET", path, "");
    const Json parsed = parse_body(reply);
    if (reply.status != 200) {
      throw std::runtime_error(parsed.get_str("error", "HTTP " + std::to_string(reply.status)));
    }
    return parsed;
  }

  Json post(const std::string& path, const std::string& payload) {
    const Reply reply = http_request(endpoint_, "POST", path, payload);
    const Json parsed = parse_body(reply);
    if (reply.status != 200) {
      throw std::runtime_error(parsed.get_str("error", "HTTP " + std::to_string(reply.status)));
    }
    return parsed;
  }

  Json fetch(const std::string& path, const Json& keep, const std::string& label) {
    try {
      return get(path);
    } catch (const std::exception& e) {
      note_error(label, e.what());
      return keep;
    }
  }

  void note_error(const std::string& label, const std::string& message) {
    for (auto& entry : errors_) {
      if (entry.first == label) {
        if (entry.second == message) return;
        entry.second = message;
        log("! " + label + ": " + message);
        return;
      }
    }
    errors_.emplace_back(label, message);
    log("! " + label + ": " + message);
  }

  void log(const std::string& line) {
    log_.push_back(line);
    if (log_.size() > 400) log_.erase(log_.begin());
  }

  void take_waterfall(const Json& image) {
    const Json* data = image.get("data");
    if (data == nullptr || data->type != Json::Type::String) return;
    const int width = static_cast<int>(image.get_num("w", 0));
    const int height = static_cast<int>(image.get_num("h", 0));
    if (width <= 0 || height <= 0) return;
    std::vector<unsigned char> bytes = base64_decode(data->text);
    if (bytes.size() < static_cast<std::size_t>(width) * static_cast<std::size_t>(height)) return;
    waterfall_ = std::move(bytes);
    waterfall_w_ = width;
    waterfall_h_ = height;
  }

  const Json& profile_json() const {
    static const Json missing;
    const Json& profiles = modes_.at("profiles");
    if (profiles.type != Json::Type::Array) return missing;
    for (const Json& profile : profiles.items) {
      if (profile.get_str("id") == profile_) return profile;
    }
    return profiles.items.empty() ? missing : profiles.items.front();
  }

  int mode_count() const { return static_cast<int>(profile_json().at("modes").length()); }

  void clamp_level() {
    const int count = mode_count();
    if (count <= 0) {
      level_ = 0;
      return;
    }
    level_ = std::min(std::max(level_, 0), count - 1);
  }

  const Json& mode_json() const {
    const Json& modes = profile_json().at("modes");
    if (modes.length() == 0) {
      static const Json missing;
      return missing;
    }
    const std::size_t index = static_cast<std::size_t>(
        std::min(std::max(level_, 0), static_cast<int>(modes.length()) - 1));
    return modes.at(index);
  }

  std::string mode_tag() const {
    const Json& mode = mode_json();
    return "L" + std::to_string(level_ + 1) + " " + mode.get_str("label", "?") + " " +
           number(mode.get_num("bps"), 0) + " bps";
  }

  std::string profile_line() const {
    return "profile " + profile_json().get_str("label", profile_) + " | " + mode_tag() +
           " | " + number(profile_json().get_num("bw_hz"), 0) + " Hz";
  }

  std::string radio_line() const {
    if (!radio_.get_bool("connected")) {
      return "radio  not connected -- d discovers an HL2; m uses the simulated channel";
    }
    std::string line = "radio  " + radio_.get_str("board", "HL2") + "  " +
                       mhz(radio_.get_num("freq_hz")) + "  " +
                       std::to_string(static_cast<int>(radio_.get_num("rate") / 1000.0)) +
                       " kHz  LNA " + std::to_string(static_cast<int>(radio_.get_num("lna_db"))) +
                       " dB  " + (radio_.get_bool("ptt") ? "TX" : "rx");
    const std::string mac = radio_.get_str("mac");
    if (!mac.empty()) line += "  " + mac;
    return line;
  }

  std::string host_line() const {
    if (services_.at("host").get_bool("running")) {
      return "host   running -- PAT and VarAC dial 8300 (commands) and 8301 (data)";
    }
    return "host   off -- s starts it";
  }

  std::string rig_line() const {
    std::string line;
    if (cat_.get_bool("connected")) {
      line = "CAT    " + mhz(cat_.get_num("freq_hz")) + "  " + cat_.get_str("mode", "?");
      if (cat_.get_bool("ptt")) line += "  TX";
      const Json* strength = cat_.get("strength");
      if (strength != nullptr && strength->type == Json::Type::Number) {
        line += "  S" + number(strength->number, 0);
      }
    } else {
      line = "CAT    no session";
    }
    if (tci_.get_bool("connected")) {
      line += "  |  TCI " + mhz(tci_.get_num("vfo_hz")) + " " + tci_.get_str("mode", "");
    }
    return line;
  }

  static char ramp(unsigned char value) {
    static const char shades[] = " .:-=+*#%@";
    return shades[static_cast<int>(value) * 9 / 255];
  }

  std::vector<std::string> render_image(int width, int rows) const {
    std::vector<std::string> out;
    if (!waterfall_.empty() && waterfall_w_ > 0 && waterfall_h_ > 0) {
      for (int row = 0; row < rows; ++row) {
        const int source_row = row * waterfall_h_ / rows;
        std::string line;
        for (int col = 0; col < width; ++col) {
          const int source_col = col * waterfall_w_ / width;
          const std::size_t index = static_cast<std::size_t>(source_row) *
                                        static_cast<std::size_t>(waterfall_w_) +
                                    static_cast<std::size_t>(source_col);
          line.push_back(ramp(waterfall_[index]));
        }
        out.push_back(line);
      }
      return out;
    }
    if (!spectrum_.empty()) {
      std::string line = "spectrum ";
      const int span = std::max(1, width - 9);
      for (int col = 0; col < span; ++col) {
        const std::size_t index = static_cast<std::size_t>(col) * spectrum_.size() /
                                  static_cast<std::size_t>(span);
        line.push_back(ramp(spectrum_[index]));
      }
      out.push_back(clip(line, static_cast<std::size_t>(width)));
      for (int row = 1; row < rows; ++row) out.push_back("");
      return out;
    }
    return out;
  }
};

std::string prompt_line(const std::string& label) {
  std::cout << "\r\n" << label << ": " << std::flush;
  std::string line;
  if (!std::getline(std::cin, line)) return {};
  return line;
}

void draw(const std::vector<std::string>& lines) {
  std::cout << "\x1b[H";
  for (const std::string& line : lines) std::cout << line << "\x1b[K\n";
  std::cout << "\x1b[J" << std::flush;
}

int run_interactive(App& app) {
  Terminal terminal;
  if (!terminal.tty()) {
    app.refresh();
    app.print_once(100, 40);
    return 0;
  }
  std::signal(SIGINT, stop_signal);
  std::signal(SIGTERM, stop_signal);
  std::cout << "\x1b[?1049h\x1b[?25l\x1b[H\x1b[J" << std::flush;

  auto last_refresh = std::chrono::steady_clock::now() - std::chrono::seconds(5);
  auto size = terminal_size();
  app.refresh();
  draw(app.render(size.first, size.second));

  while (g_stop == 0) {
    const auto now = std::chrono::steady_clock::now();
    if (now - last_refresh >= std::chrono::seconds(1)) {
      app.refresh();
      size = terminal_size();
      draw(app.render(size.first, size.second));
      last_refresh = now;
    }

    pollfd input{STDIN_FILENO, POLLIN, 0};
    const int ready = ::poll(&input, 1, 200);
    if (ready <= 0 || (input.revents & POLLIN) == 0) {
      const auto current = terminal_size();
      if (current != size) {
        size = current;
        draw(app.render(size.first, size.second));
      }
      continue;
    }

    char key = 0;
    const ssize_t got = ::read(STDIN_FILENO, &key, 1);
    if (got != 1) continue;

    bool redraw = true;
    switch (key) {
      case 'q':
        g_stop = 1;
        break;
      case 'm': {
        terminal.leave();
        std::cout << "\x1b[?25h";
        const std::string text = prompt_line("message");
        std::cout << "\x1b[?25l";
        terminal.enter();
        if (!text.empty()) {
          app.send(text);
          last_refresh = std::chrono::steady_clock::now();
        }
        break;
      }
      case 'p':
        app.cycle_profile();
        break;
      case '+':
      case '=':
        app.bump_level(1);
        break;
      case '-':
      case '_':
        app.bump_level(-1);
        break;
      case 'l': {
        terminal.leave();
        std::cout << "\x1b[?25h";
        const std::string text = prompt_line("link text (empty for a probe)");
        std::cout << "\x1b[?25l";
        terminal.enter();
        app.run_link(text);
        last_refresh = std::chrono::steady_clock::now();
        break;
      }
      case 's':
        app.toggle_host();
        break;
      case 'd':
        app.discover_and_connect();
        last_refresh = std::chrono::steady_clock::now();
        break;
      case 'x':
        app.disconnect_radio();
        break;
      case 'r':
        app.refresh();
        break;
      default:
        redraw = false;
        break;
    }
    if (redraw) {
      const auto current = terminal_size();
      size = current;
      draw(app.render(size.first, size.second));
    }
  }

  std::cout << "\x1b[?25h\x1b[?1049l" << std::flush;
  return 0;
}

void usage() {
  std::puts(
      "usage: halcyon-tui [--url http://127.0.0.1:8730] [--once] [--send TEXT] [--link TEXT]\n"
      "  --url URL      console to attach to (also HALCYON_URL)\n"
      "  --once         render one frame, print it and exit\n"
      "  --send TEXT    send a message through the console and show what it decoded\n"
      "  --link TEXT    run the console's adaptive link test\n"
      "\n"
      "The console must be running: python -m halcyon.ui --port 8730\n"
      "Keys: m message  p profile  +/- level  l link  s host  d radio  x disconnect\n"
      "      r refresh  q quit");
}

}  // namespace

int main(int argc, char** argv) {
  std::string url;
  if (const char* env = std::getenv("HALCYON_URL")) url = env;
  bool once = false;
  std::string send_text;
  std::string link_text;

  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    auto value = [&](const std::string& what) {
      if (i + 1 >= argc) throw std::runtime_error(what + " needs a value");
      return std::string(argv[++i]);
    };
    try {
      if (arg == "--url") {
        url = value("--url");
      } else if (arg == "--once") {
        once = true;
      } else if (arg == "--send") {
        send_text = value("--send");
      } else if (arg == "--link") {
        link_text = value("--link");
      } else if (arg == "--help" || arg == "-h") {
        usage();
        return 0;
      } else {
        std::cerr << "halcyon-tui: unknown argument " << arg << "\n\n";
        usage();
        return 2;
      }
    } catch (const std::exception& e) {
      std::cerr << "halcyon-tui: " << e.what() << '\n';
      return 2;
    }
  }
  if (url.empty()) url = "http://127.0.0.1:8730";

  App app;
  try {
    app.set_url(url);
  } catch (const std::exception& e) {
    std::cerr << "halcyon-tui: " << e.what() << '\n';
    return 2;
  }
  try {
    app.load_modes();
  } catch (const std::exception& e) {
    std::cerr << "halcyon-tui: cannot reach the console at " << app.url() << ": " << e.what()
              << "\nstart it with: python -m halcyon.ui --port 8730\n";
    return 1;
  }

  if (once || !send_text.empty() || !link_text.empty() || ::isatty(STDOUT_FILENO) == 0) {
    app.refresh();
    if (!send_text.empty()) app.send(send_text);
    if (!link_text.empty()) app.run_link(link_text);
    app.print_once(100, 40);
    return 0;
  }

  try {
    return run_interactive(app);
  } catch (const std::exception& e) {
    std::cout << "\x1b[?25h\x1b[?1049l" << std::flush;
    std::cerr << "halcyon-tui: " << e.what() << '\n';
    return 1;
  }
}
