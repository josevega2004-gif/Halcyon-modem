// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Halcyon contributors
//
// halcyon-gui -- the station console in a Qt 6 window.
//
// Like halcyon-tui, this is a client of the Python console's loopback JSON API
// rather than a second modem.  It needs a display; the terminal front end is
// the one for a headless or SSH session.
//
//   make -C cpp gui
//   ./cpp/build/halcyon-gui --url http://127.0.0.1:8730

#include <QApplication>
#include <QComboBox>
#include <QCoreApplication>
#include <QDoubleSpinBox>
#include <QEventLoop>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QLabel>
#include <QLineEdit>
#include <QMainWindow>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QPainter>
#include <QPaintEvent>
#include <QPen>
#include <QPainterPath>
#include <QPixmap>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSpinBox>
#include <QStatusBar>
#include <QTimer>
#include <QUrl>
#include <QVBoxLayout>
#include <QWidget>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <iostream>

namespace {

// ----------------------------------------------------------------------- JSON
QString text_of(const QJsonObject& object, const char* key, const QString& fallback = QString()) {
  const QJsonValue value = object.value(key);
  return value.isString() ? value.toString() : fallback;
}

double number_of(const QJsonObject& object, const char* key, double fallback = 0.0) {
  const QJsonValue value = object.value(key);
  return value.isDouble() ? value.toDouble() : fallback;
}

bool flag_of(const QJsonObject& object, const char* key, bool fallback = false) {
  const QJsonValue value = object.value(key);
  return value.isBool() ? value.toBool() : fallback;
}

QString fixed(double value, int precision = 1) {
  return QString::number(value, 'f', precision);
}

QString megahertz(double hz) { return fixed(hz / 1e6, 6) + " MHz"; }

QString profile_text(const QJsonObject& modes, const QString& id, int level) {
  const QJsonArray profiles = modes.value("profiles").toArray();
  for (const QJsonValue& value : profiles) {
    const QJsonObject profile = value.toObject();
    if (text_of(profile, "id") != id) continue;
    const QJsonArray rungs = profile.value("modes").toArray();
    const int index = std::clamp(level, 0, std::max(0, static_cast<int>(rungs.size()) - 1));
    const QJsonObject mode = rungs.isEmpty() ? QJsonObject() : rungs.at(index).toObject();
    return text_of(profile, "label", id) + "  |  L" + QString::number(index + 1) + " " +
           text_of(mode, "label", "?") + " " + fixed(number_of(mode, "bps"), 0) + " bps  |  " +
           fixed(number_of(profile, "bw_hz"), 0) + " Hz";
  }
  return id;
}

QString mode_text(const QJsonObject& modes, const QString& id, int level) {
  const QJsonArray profiles = modes.value("profiles").toArray();
  for (const QJsonValue& value : profiles) {
    const QJsonObject profile = value.toObject();
    if (text_of(profile, "id") != id) continue;
    const QJsonArray rungs = profile.value("modes").toArray();
    const int index = std::clamp(level, 0, std::max(0, static_cast<int>(rungs.size()) - 1));
    const QJsonObject mode = rungs.isEmpty() ? QJsonObject() : rungs.at(index).toObject();
    return "L" + QString::number(index + 1) + " " + text_of(mode, "label", "?") + " " +
           fixed(number_of(mode, "bps"), 0) + " bps";
  }
  return "L?";
}

QString radio_text(const QJsonObject& radio) {
  if (!flag_of(radio, "connected")) {
    return "radio  not connected -- Discover HL2, or send through the simulated channel";
  }
  QString line = "radio  " + text_of(radio, "board", "HL2") + "  " +
                 megahertz(number_of(radio, "freq_hz")) + "  " +
                 QString::number(std::lround(number_of(radio, "rate") / 1000.0)) + " kHz  LNA " +
                 QString::number(std::lround(number_of(radio, "lna_db"))) + " dB  " +
                 (flag_of(radio, "ptt") ? "TX" : "rx");
  const QString mac = text_of(radio, "mac");
  if (!mac.isEmpty()) line += "  " + mac;
  return line;
}

QString host_text(const QJsonObject& services) {
  const QJsonObject host = services.value("host").toObject();
  if (flag_of(host, "running")) return "host  running -- PAT/VarAC dial 8300 and 8301";
  return "host  off";
}

QString rig_text(const QJsonObject& cat, const QJsonObject& tci) {
  QString line;
  if (flag_of(cat, "connected")) {
    line = "CAT  " + megahertz(number_of(cat, "freq_hz")) + "  " + text_of(cat, "mode", "?");
    if (flag_of(cat, "ptt")) line += "  TX";
    const QJsonValue strength = cat.value("strength");
    if (strength.isDouble()) line += "  S" + fixed(strength.toDouble(), 0);
  } else {
    line = "CAT  no session";
  }
  if (flag_of(tci, "connected")) {
    line += "  |  TCI " + megahertz(number_of(tci, "vfo_hz")) + " " + text_of(tci, "mode");
  }
  return line;
}

QJsonObject decode_body(const QString& profile, int level, const QString& message) {
  QJsonObject body;
  body["profile"] = profile;
  body["mcs"] = level;
  body["text"] = message;
  body["snr"] = 9.0;
  body["channel"] = "awgn";
  body["looks"] = 1;
  return body;
}

QString decoded_line(const QJsonObject& reply) {
  const QJsonArray attempts = reply.value("attempts").toArray();
  if (attempts.isEmpty()) return "rx  no attempt in the reply";
  const QJsonObject first = attempts.first().toObject();
  if (flag_of(first, "ok")) {
    return "rx  " + text_of(first, "decoded") + "  [" +
           fixed(number_of(first, "reported_snr"), 1) + " dB]";
  }
  return "rx  no copy -- header " +
         QString(flag_of(first, "header_ok") ? "ok" : "lost") + ", " +
         QString::number(std::lround(number_of(first, "bytes"))) + " bytes";
}

// ---------------------------------------------------------------------- HTTP
class Console {
 public:
  explicit Console(QString base) : base_(std::move(base)) {}

  struct Reply {
    int status = 0;
    QJsonObject json;
    QString error;

    bool ok() const { return status == 200; }
  };

  Reply call(const QByteArray& method, const QString& path, const QJsonObject& body) {
    QNetworkRequest request{QUrl(base_ + path)};
    request.setHeader(QNetworkRequest::UserAgentHeader, "halcyon-gui/0.1");
    request.setTransferTimeout(5000);

    QNetworkReply* reply = nullptr;
    if (method == "GET") {
      reply = manager_.get(request);
    } else {
      request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
      reply = manager_.post(request, QJsonDocument(body).toJson(QJsonDocument::Compact));
    }

    QEventLoop loop;
    QObject::connect(reply, &QNetworkReply::finished, &loop, &QEventLoop::quit);
    loop.exec();

    Reply out;
    out.status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    const QByteArray payload = reply->readAll();
    const QJsonDocument document = QJsonDocument::fromJson(payload);
    if (document.isObject()) out.json = document.object();
    out.error = out.status == 0 ? reply->errorString() : text_of(out.json, "error");
    if (out.error.isEmpty() && out.status != 200) {
      out.error = "HTTP " + QString::number(out.status);
    }
    reply->deleteLater();
    return out;
  }

  QJsonObject get(const QString& path, bool* ok, QString* error) {
    const Reply reply = call("GET", path, {});
    *ok = reply.ok();
    *error = reply.error;
    return reply.json;
  }

  const QString& url() const { return base_; }

  QJsonObject post(const QString& path, const QJsonObject& body, bool* ok, QString* error) {
    const Reply reply = call("POST", path, body);
    *ok = reply.ok();
    *error = reply.error;
    return reply.json;
  }

 private:
  QString base_;
  QNetworkAccessManager manager_;
};

// --------------------------------------------------------------------- scope
// The receiver spectrum when a radio is attached, the waterfall of the last
// burst otherwise.  Both are just bytes, so one custom widget covers them.
class ScopeView : public QWidget {
 public:
  explicit ScopeView(QWidget* parent = nullptr) : QWidget(parent) { setMinimumHeight(260); }

  void show_waterfall(const QJsonObject& image) {
    const int width = static_cast<int>(number_of(image, "w"));
    const int height = static_cast<int>(number_of(image, "h"));
    const QByteArray raw = QByteArray::fromBase64(text_of(image, "data").toLatin1());
    if (width <= 0 || height <= 0 || raw.size() < width * height) return;
    QImage waterfall(width, height, QImage::Format_Grayscale8);
    for (int row = 0; row < height; ++row) {
      std::memcpy(waterfall.scanLine(row), raw.constData() + row * width,
                  static_cast<std::size_t>(width));
    }
    image_ = waterfall;
    spectrum_.clear();
    update();
  }

  void show_spectrum(const QByteArray& row) {
    spectrum_ = row;
    image_ = QImage();
    update();
  }

  void show_hint(const QString& hint) {
    hint_ = hint;
    image_ = QImage();
    spectrum_.clear();
    update();
  }

  bool has_content() const { return !image_.isNull() || !spectrum_.isEmpty(); }

 protected:
  void paintEvent(QPaintEvent*) override {
    QPainter painter(this);
    painter.fillRect(rect(), QColor(9, 13, 17));

    if (!image_.isNull()) {
      painter.drawImage(rect(), image_);
    } else if (!spectrum_.isEmpty()) {
      QPainterPath path;
      const int span = std::max(1, width());
      for (int x = 0; x < span; ++x) {
        const int index = x * spectrum_.size() / span;
        const qreal level = static_cast<unsigned char>(spectrum_.at(index)) / 255.0;
        const qreal y = height() - 6.0 - level * (height() - 12.0);
        if (x == 0) {
          path.moveTo(0, y);
        } else {
          path.lineTo(x, y);
        }
      }
      painter.setRenderHint(QPainter::Antialiasing, true);
      painter.setPen(QPen(QColor(110, 210, 155), 1.4));
      painter.drawPath(path);
    } else {
      painter.setPen(QColor(145, 160, 170));
      painter.drawText(rect().adjusted(16, 16, -16, -16), Qt::AlignCenter | Qt::TextWordWrap,
                       hint_);
    }

    painter.setPen(QColor(48, 58, 68));
    painter.drawRect(rect().adjusted(0, 0, -1, -1));
  }

 private:
  QImage image_;
  QByteArray spectrum_;
  QString hint_ =
      "no radio connected\n\nDiscover an HL2, or send a message through the simulated channel";
};

// -------------------------------------------------------------------- window
class StationWindow : public QMainWindow {
 public:
  explicit StationWindow(Console* console) : console_(console) {
    auto* central = new QWidget(this);
    auto* layout = new QVBoxLayout(central);

    profile_label_ = new QLabel("profile ...", central);
    radio_label_ = new QLabel("radio ...", central);
    host_label_ = new QLabel("host ...", central);
    rig_label_ = new QLabel("CAT ...", central);
    for (QLabel* label : {profile_label_, radio_label_, host_label_, rig_label_}) {
      label->setTextInteractionFlags(Qt::TextSelectableByMouse);
      layout->addWidget(label);
    }

    auto* panes = new QHBoxLayout();
    scope_ = new ScopeView(central);
    panes->addWidget(scope_, 3);
    transcript_ = new QPlainTextEdit(central);
    transcript_->setReadOnly(true);
    transcript_->setMinimumWidth(400);
    transcript_->setPlaceholderText("messages, notes and errors appear here");
    panes->addWidget(transcript_, 2);
    layout->addLayout(panes, 1);

    auto* rung_row = new QHBoxLayout();
    rung_row->addWidget(new QLabel("Profile", central));
    profiles_ = new QComboBox(central);
    profiles_->setMinimumWidth(190);
    rung_row->addWidget(profiles_);
    rung_row->addWidget(new QLabel("Level", central));
    levels_ = new QSpinBox(central);
    levels_->setRange(1, 1);
    levels_->setValue(1);
    rung_row->addWidget(levels_);
    rung_row->addWidget(new QLabel("Frequency", central));
    frequency_ = new QDoubleSpinBox(central);
    frequency_->setRange(0.1, 30.0);
    frequency_->setDecimals(6);
    frequency_->setSingleStep(0.001);
    frequency_->setValue(14.105);
    frequency_->setSuffix(" MHz");
    rung_row->addWidget(frequency_);
    auto* apply = new QPushButton("Apply to radio", central);
    rung_row->addWidget(apply);
    rung_row->addStretch(1);
    layout->addLayout(rung_row);

    auto* message_row = new QHBoxLayout();
    message_ = new QLineEdit(central);
    message_->setPlaceholderText("Message through the simulated channel");
    message_row->addWidget(message_, 1);
    auto* send = new QPushButton("Send", central);
    message_row->addWidget(send);
    auto* link = new QPushButton("Link test", central);
    message_row->addWidget(link);
    layout->addLayout(message_row);

    auto* action_row = new QHBoxLayout();
    auto* discover = new QPushButton("Discover HL2", central);
    action_row->addWidget(discover);
    auto* disconnect = new QPushButton("Disconnect radio", central);
    action_row->addWidget(disconnect);
    host_button_ = new QPushButton("Start host", central);
    action_row->addWidget(host_button_);
    auto* refresh_button = new QPushButton("Refresh", central);
    action_row->addWidget(refresh_button);
    action_row->addStretch(1);
    layout->addLayout(action_row);

    setCentralWidget(central);
    resize(1120, 760);
    statusBar()->showMessage("ready");

    connect(send, &QPushButton::clicked, this, [this]() { send_message(message_->text()); });
    connect(message_, &QLineEdit::returnPressed, this,
            [this]() { send_message(message_->text()); });
    connect(link, &QPushButton::clicked, this, [this]() { run_link(message_->text()); });
    connect(discover, &QPushButton::clicked, this, [this]() { discover_radio(); });
    connect(disconnect, &QPushButton::clicked, this, [this]() { disconnect_radio(); });
    connect(host_button_, &QPushButton::clicked, this, [this]() { toggle_host(); });
    connect(refresh_button, &QPushButton::clicked, this, [this]() { refresh(); });
    connect(apply, &QPushButton::clicked, this, [this]() { apply_radio(); });
    connect(profiles_, &QComboBox::currentIndexChanged, this,
            [this](int) { profile_changed(); });
    connect(levels_, &QSpinBox::valueChanged, this, [this](int) { update_labels(); });

    timer_ = new QTimer(this);
    connect(timer_, &QTimer::timeout, this, [this]() { refresh(); });
    timer_->start(1000);
  }

  bool load_modes() {
    bool ok = false;
    QString error;
    const QJsonObject modes = console_->get("/api/modes", &ok, &error);
    if (!ok) {
      append("! console: " + error);
      statusBar()->showMessage("console offline: " + error);
      return false;
    }
    modes_ = modes;
    const QJsonArray profiles = modes.value("profiles").toArray();
    profiles_->blockSignals(true);
    profiles_->clear();
    for (const QJsonValue& value : profiles) {
      const QJsonObject profile = value.toObject();
      profiles_->addItem(text_of(profile, "label") + "  ·  " + text_of(profile, "band"),
                         text_of(profile, "id"));
    }
    profiles_->blockSignals(false);
    if (profiles_->count() > 0) profiles_->setCurrentIndex(0);
    profile_changed();
    return true;
  }

  void refresh() {
    bool ok = false;
    QString error;

    const QJsonObject radio = console_->get("/api/radio", &ok, &error);
    if (!ok) {
      statusBar()->showMessage("console offline: " + error);
      radio_label_->setText("radio  console offline");
      return;
    }
    statusBar()->showMessage("console " + console_->url());
    radio_ = radio;
    services_ = console_->get("/api/services", &ok, &error);
    if (!ok) services_ = QJsonObject();
    cat_ = console_->get("/api/cat", &ok, &error);
    if (!ok) cat_ = QJsonObject();
    tci_ = console_->get("/api/tci", &ok, &error);
    if (!ok) tci_ = QJsonObject();

    if (flag_of(radio_, "connected")) {
      const QJsonObject spectrum = console_->get("/api/radio/spectrum", &ok, &error);
      if (ok && spectrum.value("row").isString()) {
        scope_->show_spectrum(QByteArray::fromBase64(spectrum.value("row").toString().toLatin1()));
      }
    } else if (!scope_->has_content()) {
      scope_->show_hint(
          "no radio connected\n\nDiscover an HL2, or send a message through the simulated "
          "channel");
    }
    update_labels();
  }

  void send_message(const QString& message) {
    if (message.trimmed().isEmpty()) return;
    const QString profile = profile_id();
    const int level = levels_->value() - 1;
    append("tx  " + message + "  [" + mode_text(modes_, profile, level) + "]");
    bool ok = false;
    QString error;
    const QJsonObject reply = console_->post("/api/decode",
                                             decode_body(profile, level, message), &ok, &error);
    if (!ok) {
      append("! send: " + error);
      return;
    }
    append(decoded_line(reply));
    if (reply.value("waterfall").isObject()) {
      scope_->show_waterfall(reply.value("waterfall").toObject());
    }
    if (flag_of(reply, "trimmed")) append("! message trimmed to the burst capacity");
    message_->clear();
  }

  void run_link(const QString& message) {
    const QString payload = message.trimmed().isEmpty() ? QString("cq de halcyon") : message;
    const QString profile = profile_id();
    const int level = levels_->value() - 1;
    append("link " + payload + "  [" + mode_text(modes_, profile, level) + "]");
    QJsonObject body;
    body["profile"] = profile;
    body["mcs"] = level;
    body["text"] = payload;
    body["snr"] = 9.0;
    body["adaptive"] = true;
    bool ok = false;
    QString error;
    const QJsonObject reply = console_->post("/api/link", body, &ok, &error);
    if (!ok) {
      append("! link: " + error);
      return;
    }
    append("link " + QString(flag_of(reply, "complete") ? "complete" : "failed") + ": " +
           fixed(number_of(reply, "goodput_bps"), 1) + " bps, " +
           QString::number(std::lround(number_of(reply, "frames"))) + " frames, " +
           QString::number(std::lround(number_of(reply, "retransmissions"))) +
               " retransmissions");
  }

  void toggle_host() {
    bool ok = false;
    QString error;
    const QJsonObject services = console_->get("/api/services", &ok, &error);
    if (!ok) {
      append("! host: " + error);
      return;
    }
    services_ = services;
    const bool running = flag_of(services.value("host").toObject(), "running");
    const char* env_call = std::getenv("HALCYON_MYCALL");
    const QString mycall =
        env_call != nullptr && env_call[0] != '\0' ? QString::fromLocal8Bit(env_call) : "N0CALL";
    const QJsonObject reply =
        running ? console_->post("/api/services/stop", {}, &ok, &error)
                : console_->post("/api/services/start",
                                 QJsonObject{{"mycall", mycall},
                                             {"bandwidth", 2300},
                                             {"snr", 9.0},
                                             {"channel", "awgn"}},
                                 &ok, &error);
    if (!ok) {
      append("! host: " + error);
      return;
    }
    services_ = reply;
    append(running ? "host stopped" : "host listening on 8300/8301");
    update_labels();
  }

  void discover_radio() {
    bool ok = false;
    QString error;
    append("discovery: broadcasting to 255.255.255.255:1024");
    const QJsonObject found = console_->post("/api/radio/discover", {}, &ok, &error);
    if (!ok) {
      append("! radio: " + error);
      return;
    }
    const QJsonArray radios = found.value("radios").toArray();
    if (radios.isEmpty()) {
      append("! no radio answered -- check power, cable and subnet");
      return;
    }
    const QJsonObject radio = radios.first().toObject();
    QJsonObject body;
    body["ip"] = text_of(radio, "ip");
    body["rate"] = 48000;
    body["freq_hz"] = static_cast<double>(std::llround(frequency_->value() * 1e6));
    body["lna_db"] = 16;
    body["drive"] = 8;
    for (const char* key : {"mac", "board", "firmware"}) {
      const QString value = text_of(radio, key);
      if (!value.isEmpty()) body[key] = value;
    }
    const QJsonObject status = console_->post("/api/radio/connect", body, &ok, &error);
    if (!ok) {
      append("! radio: " + error);
      return;
    }
    radio_ = status;
    append("radio connected: " + text_of(status, "board", "HL2") + " " +
           megahertz(number_of(status, "freq_hz")) + " -- receive only, keying stays on the page");
    refresh();
  }

  void disconnect_radio() {
    bool ok = false;
    QString error;
    radio_ = console_->post("/api/radio/disconnect", {}, &ok, &error);
    if (!ok) {
      append("! radio: " + error);
      return;
    }
    scope_->show_hint(
        "no radio connected\n\nDiscover an HL2, or send a message through the simulated channel");
    append("radio disconnected");
    update_labels();
  }

  void apply_radio() {
    if (!flag_of(radio_, "connected")) {
      append("! tune: connect a radio first");
      return;
    }
    bool ok = false;
    QString error;
    const QJsonObject status = console_->post(
        "/api/radio/set",
        QJsonObject{{"freq_hz", static_cast<double>(std::llround(frequency_->value() * 1e6))}},
        &ok, &error);
    if (!ok) {
      append("! tune: " + error);
      return;
    }
    radio_ = status;
    append("tuned to " + megahertz(number_of(status, "freq_hz")));
    update_labels();
  }

  void append(const QString& line) { transcript_->appendPlainText(line); }

 private:
  QString profile_id() const {
    const QString id = profiles_->currentData().toString();
    return id.isEmpty() ? QString("wide") : id;
  }

  void profile_changed() {
    const QJsonArray profiles = modes_.value("profiles").toArray();
    for (const QJsonValue& value : profiles) {
      const QJsonObject profile = value.toObject();
      if (text_of(profile, "id") != profile_id()) continue;
      const int count = static_cast<int>(profile.value("modes").toArray().size());
      levels_->blockSignals(true);
      levels_->setRange(1, std::max(1, count));
      levels_->setValue(std::min(5, std::max(1, count)));
      levels_->blockSignals(false);
      break;
    }
    update_labels();
  }

  void update_labels() {
    profile_label_->setText("profile  " + profile_text(modes_, profile_id(), levels_->value() - 1));
    radio_label_->setText(radio_text(radio_));
    host_label_->setText(host_text(services_));
    rig_label_->setText(rig_text(cat_, tci_));
    host_button_->setText(flag_of(services_.value("host").toObject(), "running") ? "Stop host"
                                                                                 : "Start host");
  }

  Console* console_;
  QJsonObject modes_;
  QJsonObject radio_;
  QJsonObject services_;
  QJsonObject cat_;
  QJsonObject tci_;
  QLabel* profile_label_ = nullptr;
  QLabel* radio_label_ = nullptr;
  QLabel* host_label_ = nullptr;
  QLabel* rig_label_ = nullptr;
  ScopeView* scope_ = nullptr;
  QPlainTextEdit* transcript_ = nullptr;
  QComboBox* profiles_ = nullptr;
  QSpinBox* levels_ = nullptr;
  QDoubleSpinBox* frequency_ = nullptr;
  QLineEdit* message_ = nullptr;
  QPushButton* host_button_ = nullptr;
  QTimer* timer_ = nullptr;
};

// --------------------------------------------------------------------- modes
int run_check(const QString& url, const QString& send_text) {
  Console console(url);
  bool ok = false;
  QString error;
  const QJsonObject modes = console.get("/api/modes", &ok, &error);
  if (!ok) {
    std::cerr << "halcyon-gui: cannot reach the console at " << url.toStdString() << ": "
              << error.toStdString() << "\nstart it with: python -m halcyon.ui --port 8730\n";
    return 1;
  }

  const QJsonArray profiles = modes.value("profiles").toArray();
  QString profile = "wide";
  int level = 4;
  if (!profiles.isEmpty()) {
    const QJsonObject first = profiles.first().toObject();
    profile = text_of(first, "id", "wide");
    level = std::clamp(4, 0, std::max(0, static_cast<int>(first.value("modes").toArray().size()) - 1));
  }

  const QJsonObject radio = console.get("/api/radio", &ok, &error);
  const QJsonObject services = console.get("/api/services", &ok, &error);
  const QJsonObject cat = console.get("/api/cat", &ok, &error);
  const QJsonObject tci = console.get("/api/tci", &ok, &error);

  std::cout << "console: " << url.toStdString() << "\n";
  std::cout << "modes: " << profiles.size() << " profiles\n";
  std::cout << "profile: " << profile_text(modes, profile, level).toStdString() << "\n";
  std::cout << radio_text(radio).toStdString() << "\n";
  std::cout << host_text(services).toStdString() << "\n";
  std::cout << rig_text(cat, tci).toStdString() << "\n";

  if (!send_text.isEmpty()) {
    std::cout << "tx  " << send_text.toStdString() << "  ["
              << mode_text(modes, profile, level).toStdString() << "]\n";
    const QJsonObject reply =
        console.post("/api/decode", decode_body(profile, level, send_text), &ok, &error);
    if (!ok) {
      std::cerr << "halcyon-gui: send failed: " << error.toStdString() << "\n";
      return 1;
    }
    std::cout << decoded_line(reply).toStdString() << "\n";
  }
  return 0;
}

int run_window(const QString& url, const QString& screenshot, const QString& send_text) {
  Console console(url);
  StationWindow window(&console);
  if (!window.load_modes()) return 1;
  window.refresh();
  if (!send_text.isEmpty()) window.send_message(send_text);
  window.show();

  if (!screenshot.isEmpty()) {
    QTimer::singleShot(1200, &window, [&window, screenshot]() {
      const QPixmap shot = window.grab();
      if (shot.isNull() || !shot.save(screenshot, "PNG")) {
        std::cerr << "halcyon-gui: could not save " << screenshot.toStdString() << "\n";
        QCoreApplication::exit(1);
        return;
      }
      QCoreApplication::quit();
    });
  }
  return QApplication::exec();
}

void usage() {
  std::puts(
      "usage: halcyon-gui [--url http://127.0.0.1:8730] [--check] [--send TEXT]\n"
      "                   [--screenshot FILE]\n"
      "  --url URL        console to attach to (also HALCYON_URL)\n"
      "  --check          print the console state and exit, no window\n"
      "  --send TEXT      send a message and show what it decoded\n"
      "  --screenshot F   draw the window, save it as PNG, then exit\n"
      "\n"
      "The console must be running: python -m halcyon.ui --port 8730\n"
      "On a machine with no display, use halcyon-tui instead.");
}

}  // namespace

int main(int argc, char** argv) {
  QString url;
  if (const char* env = std::getenv("HALCYON_URL")) url = QString::fromLocal8Bit(env);
  bool check = false;
  QString send_text;
  QString screenshot;

  for (int i = 1; i < argc; ++i) {
    const QString argument = QString::fromLocal8Bit(argv[i]);
    auto value = [&](const QString& what) -> QString {
      if (i + 1 >= argc) {
        std::cerr << "halcyon-gui: " << what.toStdString() << " needs a value\n";
        std::exit(2);
      }
      return QString::fromLocal8Bit(argv[++i]);
    };
    if (argument == "--url") {
      url = value("--url");
    } else if (argument == "--check") {
      check = true;
    } else if (argument == "--send") {
      send_text = value("--send");
    } else if (argument == "--screenshot") {
      screenshot = value("--screenshot");
    } else if (argument == "--help" || argument == "-h") {
      usage();
      return 0;
    } else {
      std::cerr << "halcyon-gui: unknown argument " << argument.toStdString() << "\n\n";
      usage();
      return 2;
    }
  }
  if (url.isEmpty()) url = "http://127.0.0.1:8730";

  if (check) {
    QCoreApplication application(argc, argv);
    return run_check(url, send_text);
  }

  QApplication application(argc, argv);
  QApplication::setApplicationName("Halcyon console");
  return run_window(url, screenshot, send_text);
}
