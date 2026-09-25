(() => {
  const configNode = document.getElementById("room-config");
  if (!configNode) return;
  const config = JSON.parse(configNode.textContent);
  const isHost = config.role === "host";
  const $ = (selector) => document.querySelector(selector);
  const wsUrl = (path) => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;

  const localVideo = $("#local-video");
  const remoteVideo = $("#remote-video");
  const localPlaceholder = $("#local-placeholder");
  const remotePlaceholder = $("#remote-placeholder");
  const connectionLabel = $("#connection-label");
  const roomStatus = $(".room-status");
  const startButton = $("#start-media");
  const prejoinCard = $("#prejoin-card");
  const micButton = $("#toggle-mic");
  const cameraButton = $("#toggle-camera");
  const leaveButton = $("#leave-room");
  const meetingStage = $("#meeting-stage");
  const videoGrid = $("#video-grid");
  const emotionOverlay = $("#emotion-overlay");

  let localStream = null;
  let peer = null;
  let signalSocket = null;
  let asrSocket = null;
  let audioContext = null;
  let audioProcessor = null;
  let startedAt = null;
  let timerHandle = null;
  let makingOffer = false;
  let questionTimer = null;
  let questionInFlight = false;
  let lastQuestionAt = 0;
  let candidateRevision = 0;
  let generatedRevision = 0;
  let analysisEnabled = false;
  let frameTimer = null;
  let staleTimer = null;
  let frameInFlight = false;
  let captionTimer = null;
  let stopping = false;
  let reconnectTimer = null;
  let leaving = false;
  let flushRequest = null;
  let finishCaptureResolve = null;
  let peerFlushResolve = null;
  let captureHealthTimer = null;
  let lastAudioPacketAt = 0;
  let latestRms = 0;
  let sentAudioSeconds = 0;
  let lastOwnTranscriptAt = 0;
  let activeParticipant = "remote";
  let manualSpeakerUntil = 0;
  const captionHistory = [];
  const seenSpans = new Set();
  const frameCanvas = document.createElement("canvas");
  frameCanvas.width = 640;
  frameCanvas.height = 480;

  function setConnection(label, connected = false) {
    if (connectionLabel) connectionLabel.textContent = label;
    if (roomStatus) roomStatus.classList.toggle("connected", connected);
  }

  function remember(key, value) {
    try { sessionStorage.setItem(`meeting:${config.interviewId}:${key}`, value); } catch (_) { /* optional preference */ }
  }

  function recall(key) {
    try { return sessionStorage.getItem(`meeting:${config.interviewId}:${key}`); } catch (_) { return null; }
  }

  function setActiveParticipant(participant, manual = false) {
    if (!videoGrid || !["local", "remote"].includes(participant)) return;
    activeParticipant = participant;
    if (manual) manualSpeakerUntil = Date.now() + 15000;
    videoGrid.querySelectorAll(".video-tile").forEach((tile) => {
      tile.classList.toggle("is-active-speaker", tile.dataset.participant === participant);
    });
  }

  function updateActiveSpeaker(speaker) {
    if (Date.now() < manualSpeakerUntil) return;
    const participant = speaker === "candidate"
      ? (isHost ? "remote" : "local")
      : speaker === "interviewer" ? (isHost ? "local" : "remote") : null;
    if (participant) setActiveParticipant(participant);
  }

  function setViewMode(mode, persist = true) {
    if (!videoGrid || !["gallery", "speaker"].includes(mode)) return;
    videoGrid.dataset.viewMode = mode;
    document.querySelectorAll("button[data-view-mode]").forEach((button) => {
      const selected = button.dataset.viewMode === mode;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    if (mode === "speaker") setActiveParticipant(activeParticipant);
    if (persist) remember("view", mode);
  }

  function syncFullscreenButton() {
    const button = $("#toggle-fullscreen");
    if (!button) return;
    const active = document.fullscreenElement === meetingStage;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    const label = $("#fullscreen-label");
    if (label) label.textContent = active ? "Küçült" : "Tam ekran";
  }

  function setRemoteState(online) {
    const chip = $("#remote-signal");
    if (chip) chip.textContent = online ? "Bağlanıyor" : "Çevrimdışı";
    const dot = $(".remote-tile .quality-dot");
    if (dot) dot.classList.toggle("muted", !online);
    if (!online) {
      remotePlaceholder?.classList.remove("hidden");
      const label = $("#remote-placeholder-text");
      if (label) label.textContent = "Diğer katılımcı bekleniyor";
    }
  }

  function createPeer() {
    if (peer) return peer;
    peer = new RTCPeerConnection({ iceServers: config.iceServers });
    if (localStream) {
      localStream.getTracks().forEach((track) => peer.addTrack(track, localStream));
    }
    peer.onicecandidate = ({ candidate }) => {
      if (candidate) sendSignal({ type: "ice", candidate });
    };
    peer.ontrack = ({ streams }) => {
      if (!streams[0]) return;
      remoteVideo.srcObject = streams[0];
      remotePlaceholder?.classList.add("hidden");
      const chip = $("#remote-signal");
      if (chip) chip.textContent = "Canlı";
    };
    peer.onconnectionstatechange = () => {
      const state = peer.connectionState;
      if (state === "connected") {
        setConnection("Görüşme bağlı", true);
        setRemoteState(true);
        const chip = $("#remote-signal");
        if (chip) chip.textContent = "Canlı";
      } else if (["failed", "disconnected", "closed"].includes(state)) {
        setConnection(state === "failed" ? "Bağlantı kurulamadı" : "Bağlantı kesildi");
        setRemoteState(false);
      } else {
        setConnection("Güvenli bağlantı kuruluyor");
      }
    };
    return peer;
  }

  function sendSignal(payload) {
    if (signalSocket?.readyState === WebSocket.OPEN) {
      signalSocket.send(JSON.stringify(payload));
    }
  }

  async function makeOffer() {
    const currentPeer = createPeer();
    if (makingOffer || currentPeer.signalingState !== "stable") return;
    try {
      makingOffer = true;
      await currentPeer.setLocalDescription(await currentPeer.createOffer());
      sendSignal({ type: "offer", sdp: currentPeer.localDescription });
    } finally {
      makingOffer = false;
    }
  }

  function connectSignaling() {
    signalSocket = new WebSocket(wsUrl(config.signalPath));
    signalSocket.onopen = () => {
      setConnection("Katılımcı bekleniyor");
      sendSignal({ type: "ready" });
    };
    signalSocket.onmessage = async (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "capture-flushed" && message.role === "candidate") {
        peerFlushResolve?.(Boolean(message.ok));
        return;
      }
      if (message.type === "prepare-close" && message.role === "host" && !isHost && !leaving) {
        leaving = true;
        setConnection("Görüşme bitiriliyor");
        const ok = await flushCapture();
        sendSignal({type: "capture-flushed", ok});
        leaving = false;
        return;
      }
      if (message.type === "presence") {
        const online = message.state === "joined";
        setRemoteState(online);
        if (online && isHost && message.role === "candidate") await makeOffer();
        if (online && !isHost && message.role === "host") sendSignal({ type: "ready" });
        return;
      }
      if (message.type === "ready" && isHost && message.role === "candidate") {
        await makeOffer();
        return;
      }
      const currentPeer = createPeer();
      if (message.type === "offer") {
        await currentPeer.setRemoteDescription(message.sdp);
        await currentPeer.setLocalDescription(await currentPeer.createAnswer());
        sendSignal({ type: "answer", sdp: currentPeer.localDescription });
      } else if (message.type === "answer") {
        if (currentPeer.signalingState === "have-local-offer") {
          await currentPeer.setRemoteDescription(message.sdp);
        }
      } else if (message.type === "ice" && message.candidate) {
        try { await currentPeer.addIceCandidate(message.candidate); } catch (_) { /* stale ICE */ }
      } else if (message.type === "media-state") {
        const chip = $("#remote-signal");
        if (chip && !message.camera) chip.textContent = "Kamera kapalı";
        if (isHost && $("#video-quality")) $("#video-quality").textContent = message.camera ? "Görünür" : "Veri yok";
      }
    };
    signalSocket.onclose = () => setConnection("Sinyal bağlantısı kapandı");
    signalSocket.onerror = () => setConnection("Bağlantı hatası");
  }

  function updateAsrBadge(message) {
    const badge = $("#asr-badge");
    if (!badge) return;
    badge.className = `service-badge state-${message.state || "disabled"}`;
    badge.innerHTML = "<i></i>";
    badge.append(document.createTextNode(message.label || "Transkripsiyon"));
  }

  function connectAsr() {
    if (stopping) return;
    asrSocket = new WebSocket(wsUrl(config.asrPath));
    asrSocket.onopen = () => {};
    asrSocket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "flushed" && flushRequest?.id === message.request_id) {
        flushRequest.resolve(message.dropped_audio_seconds === 0);
        return;
      }
      if (message.type === "service-status" && message.service === "asr") {
        updateAsrBadge(message);
        if (["error", "disabled", "missing", "degraded"].includes(message.state)) captionNotice(message.label);
      }
      if (message.type === "transcript" && !seenSpans.has(message.span_id)) {
        seenSpans.add(message.span_id);
        if (seenSpans.size > 500) seenSpans.delete(seenSpans.values().next().value);
        showCaption(message);
        updateActiveSpeaker(message.speaker);
        if (message.speaker === (isHost ? "interviewer" : "candidate")) {
          lastOwnTranscriptAt = Date.now();
        }
        if (isHost) {
          if (message.is_final && message.speaker === "candidate") {
            candidateRevision += 1;
            scheduleQuestionGeneration();
          }
        }
      }
      if (message.type === "analysis-status") {
        const status = $("#analysis-status");
        const modelStates = {ready: "Hazır", available: "İndirilmiş", missing: "Model eksik", disabled: "Kapalı", error: "Yükleme hatası"};
        for (const [modality, model] of Object.entries(message.models || {})) {
          if (!["text", "audio", "video"].includes(modality)) continue;
          const state = document.querySelector(`[data-modality="${modality}"] .model-state`);
          if (state) state.textContent = modelStates[model.state] || "Bekleniyor";
        }
        if (isHost) {
          analysisEnabled = false;
          if (status) status.textContent = message.allowed ? "Aday bağlantısı bekleniyor" : "Aday analiz izni vermedi";
          resetEmotions(message.allowed ? "Aday bekleniyor" : "İzin yok");
          return;
        }
        const allowedHere = Boolean(config.candidateAnalysisConsent);
        const lateEnable = Boolean(message.enabled && !allowedHere);
        analysisEnabled = Boolean(message.enabled && message.allowed && allowedHere && !stopping);
        clearInterval(frameTimer);
        if (analysisEnabled) { sendFrame(); frameTimer = setInterval(sendFrame, 8000); }
        if (lateEnable && asrSocket?.readyState === WebSocket.OPEN) {
          asrSocket.send(JSON.stringify({type: "analysis-consent", enabled: false}));
        }
      }
      if (message.type === "candidate-analysis-status" && isHost) {
        analysisEnabled = Boolean(message.enabled && message.allowed && !stopping);
        const status = $("#analysis-status");
        if (status) status.textContent = analysisEnabled ? "Canlı · aday izni var" : (message.allowed ? "Aday bağlantısı bekleniyor" : "Aday analiz izni vermedi");
        if (!analysisEnabled) resetEmotions(message.allowed ? "Aday bekleniyor" : "İzin yok");
      }
      if (message.type === "candidate-emotion" && isHost && analysisEnabled) renderEmotion(message);
      if (message.type === "room-closed") {
        stopMedia();
        setConnection("Görüşme tamamlandı");
        captionNotice("Görüşme sahibi oturumu bitirdi.");
      }
    };
    asrSocket.onclose = (event) => {
      analysisEnabled = false;
      clearInterval(frameTimer);
      resetEmotions(isHost ? "Aday bağlantısı yok" : "Analiz kapalı");
      if (event.code === 4403) {
        stopMedia();
        setConnection("Oturum erişimi sona erdi");
        captionNotice("Görüşme kapandı veya katılım izni sona erdi.");
        return;
      }
      if (!stopping) {
        captionNotice("Altyazı bağlantısı kesildi · yeniden bağlanılıyor");
        reconnectTimer = setTimeout(connectAsr, 3000);
      }
    };
  }

  async function startAudioCapture() {
    if (!localStream?.getAudioTracks().length || !asrSocket || audioContext) return;
    audioContext = new (window.AudioContext || window.webkitAudioContext)();
    await audioContext.resume();
    try {
      await audioContext.audioWorklet.addModule(config.audioWorkletUrl);
    } catch (_) {
      captionNotice("Tarayıcı ses işleyicisi açılamadı. Güncel bir tarayıcı ve HTTPS/localhost kullanın.");
      return;
    }
    const source = audioContext.createMediaStreamSource(localStream);
    audioProcessor = new AudioWorkletNode(audioContext, "pcm-capture");
    const silentGain = audioContext.createGain();
    silentGain.gain.value = 0;
    audioProcessor.port.onmessage = (event) => {
      if (event.data?.type === "capture-finished") { finishCaptureResolve?.(); return; }
      const samples = new Int16Array(event.data);
      let energy = 0;
      for (let i = 0; i < samples.length; i += 1) energy += samples[i] * samples[i];
      const rms = Math.sqrt(energy / samples.length) / 32768;
      latestRms = Math.max(rms, latestRms * .94);
      lastAudioPacketAt = Date.now();
      if ($("#microphone-level")) $("#microphone-level").value = Math.min(1, latestRms * 12);
      if (isHost && $("#audio-quality")) $("#audio-quality").textContent = rms > .01 ? "İyi" : "Düşük seviye";
      if (asrSocket?.readyState === WebSocket.OPEN && asrSocket.bufferedAmount < 96000) {
        // Muted tracks produce zero samples; preserve timeline without transcribing speech.
        asrSocket.send(event.data);
        sentAudioSeconds += samples.length / 16000;
      }
    };
    source.connect(audioProcessor);
    audioProcessor.connect(silentGain);
    silentGain.connect(audioContext.destination);
    staleTimer = setInterval(() => {
      document.querySelectorAll(".emotion-card").forEach((card) => {
        if (card.dataset.updatedAt && Date.now() - Number(card.dataset.updatedAt) > 20000) {
          card.classList.add("is-stale");
          card.querySelector(".emotion-label").textContent = "Yeni veri bekleniyor";
          card.querySelector(".emotion-scores").replaceChildren();
          card.querySelector(".emotion-detail").textContent = "Önceki tahmin artık güncel değil.";
          card.querySelector(".emotion-latency").textContent = "";
          delete card.dataset.updatedAt;
        }
      });
    }, 2000);
  }

  function updateCaptureHealth() {
    const audio = localStream?.getAudioTracks()[0];
    const video = localStream?.getVideoTracks()[0];
    const health = $("#capture-health");
    const fresh = Date.now() - lastAudioPacketAt < 2000;
    if ($("#microphone-level")) $("#microphone-level").value = fresh ? Math.min(1, latestRms * 12) : 0;
    if (health) health.textContent = stopping ? "Ses gönderimi durduruldu." : !audio ? "Mikrofon yok." : !audio.enabled ? "Bu sekmede mikrofon kapalı." : audio.muted || audio.readyState !== "live" ? "Tarayıcı mikrofon akışını durdurdu. Diğer sekmeyi ve cihaz iznini kontrol edin." : audioContext?.state !== "running" ? "Tarayıcı ses işlemeyi başlatmadı. Aşağıdaki düğmeyle sürdürün." : !fresh ? "Mikrofon bağlı ama ses paketi gelmiyor." : latestRms >= .004 ? "Bu sekmeye ses geliyor." : "Akış açık · şu anda sessizlik veya düşük seviye.";
    if ($("#capture-device")) $("#capture-device").textContent = `Mikrofon: ${audio?.label || "seçilmedi"} · Kamera: ${video?.enabled && !video.muted && video.readyState === "live" ? "akış açık" : "kapalı / duraklatıldı"}`;
    if ($("#capture-delivery")) $("#capture-delivery").textContent = `Gönderilen ses: ${sentAudioSeconds.toFixed(1)} sn · ${asrSocket?.readyState === WebSocket.OPEN ? "sunucu bağlantısı açık" : "sunucu bağlantısı yok"}`;
    if ($("#own-transcript-status")) $("#own-transcript-status").textContent = lastOwnTranscriptAt ? `Kendi konuşmanızdan son altyazı: ${Math.floor((Date.now() - lastOwnTranscriptAt) / 1000)} sn önce` : "Bu sekmenin konuşmasından henüz altyazı gelmedi.";
    if ($("#resume-audio")) $("#resume-audio").hidden = stopping || !audioContext || audioContext.state === "running" || audioContext.state === "closed";
  }

  $("#resume-audio")?.addEventListener("click", async () => {
    try { await audioContext?.resume(); } catch (_) { captionNotice("Ses işlemeyi sürdürmek için sayfayı yenileyip mikrofon iznini kontrol edin."); }
    updateCaptureHealth();
  });

  function requestMedia(constraints, timeoutMs) {
    let expired = false;
    let timer = null;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        expired = true;
        reject(new DOMException("Media permission timed out", "TimeoutError"));
      }, timeoutMs);
    });
    const request = navigator.mediaDevices.getUserMedia(constraints).then((stream) => {
      clearTimeout(timer);
      if (expired) {
        stream.getTracks().forEach((track) => track.stop());
        throw new DOMException("Late media stream discarded", "TimeoutError");
      }
      return stream;
    }).catch((error) => {
      clearTimeout(timer);
      throw error;
    });
    return Promise.race([request, timeout]);
  }

  function mediaErrorText(error) {
    if (error?.name === "TimeoutError") return "Safari izin isteğini sonuçlandırmadı. Adres çubuğundaki kamera simgesinden izinleri kontrol edip tekrar deneyin.";
    if (error?.name === "NotAllowedError" || error?.name === "SecurityError") return "Mikrofon erişimi engellendi. Safari site ayarlarından mikrofon iznini ‘İzin Ver’ yapıp tekrar deneyin.";
    if (error?.name === "NotFoundError") return "Kullanılabilir mikrofon bulunamadı. Bir mikrofon bağlayıp tekrar deneyin.";
    if (error?.name === "NotReadableError" || error?.name === "AbortError") return "Mikrofon başka bir uygulama tarafından kullanılıyor. Diğer görüşme uygulamalarını kapatıp tekrar deneyin.";
    return "Mikrofon başlatılamadı. Safari site izinlerini ve sistem gizlilik ayarlarını kontrol edin.";
  }

  async function startMedia() {
    startButton.disabled = true;
    startButton.textContent = "İzin bekleniyor…";
    try {
      localStream = await requestMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 24, max: 30 } },
      }, 12000);
    } catch (cameraError) {
      startButton.textContent = "Yalnız mikrofon deneniyor…";
      try {
        localStream = await requestMedia({
          audio: { echoCancellation: true, noiseSuppression: true }, video: false,
        }, 10000);
      } catch (audioError) {
        startButton.disabled = false;
        startButton.textContent = "Tekrar dene";
        const detail = prejoinCard.querySelector("p");
        if (detail) detail.textContent = mediaErrorText(audioError);
        return;
      }
    }

    localVideo.srcObject = localStream;
    captureHealthTimer = setInterval(updateCaptureHealth, 1000);
    updateCaptureHealth();
    if (localStream.getVideoTracks().length) localPlaceholder?.classList.add("hidden");
    else if (localPlaceholder) localPlaceholder.querySelector("p").textContent = "Kamera kapalı · Ses bağlı";
    prejoinCard.hidden = true;
    prejoinCard.style.display = "none";
    prejoinCard.remove();
    micButton.disabled = false;
    cameraButton.disabled = false;
    if (!localStream.getVideoTracks().length) cameraButton.classList.add("off");
    if (isHost && $("#video-quality")) $("#video-quality").textContent = localStream.getVideoTracks().length ? "Görünür" : "Veri yok";
    createPeer();
    connectSignaling();
    connectAsr();
    asrSocket.addEventListener("open", startAudioCapture, { once: true });
    startTimer();
  }

  function toggleTrack(kind, button) {
    const track = kind === "audio" ? localStream?.getAudioTracks()[0] : localStream?.getVideoTracks()[0];
    if (!track) return;
    track.enabled = !track.enabled;
    button.classList.toggle("off", !track.enabled);
    button.querySelector("small").textContent = kind === "audio"
      ? (track.enabled ? "Mikrofon" : "Sessiz")
      : (track.enabled ? "Kamera" : "Kapalı");
    if (kind === "video") {
      localPlaceholder?.classList.toggle("hidden", track.enabled);
      if (isHost && $("#video-quality")) $("#video-quality").textContent = track.enabled ? "Görünür" : "Veri yok";
    }
    sendSignal({
      type: "media-state",
      microphone: localStream?.getAudioTracks()[0]?.enabled || false,
      camera: localStream?.getVideoTracks()[0]?.enabled || false,
    });
    if (asrSocket?.readyState === WebSocket.OPEN) asrSocket.send(JSON.stringify({type: "media-state", camera: localStream?.getVideoTracks()[0]?.enabled || false}));
    if (kind === "video" && !track.enabled) renderEmotion({modality: "video", state: "no_data", detail: "Kamera kapalı.", scores: []});
  }

  function startTimer() {
    startedAt = Date.now();
    const timer = $("#room-timer");
    timerHandle = setInterval(() => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      const minutes = Math.floor(seconds / 60).toString().padStart(2, "0");
      const remain = (seconds % 60).toString().padStart(2, "0");
      if (timer) timer.textContent = `${minutes}:${remain}`;
    }, 1000);
  }

  function scheduleQuestionGeneration() {
    clearTimeout(questionTimer);
    const quietWindow = 4500;
    const energyWindow = Math.max(0, 18000 - (Date.now() - lastQuestionAt));
    questionTimer = setTimeout(() => {
      questionTimer = null;
      generateQuestions(false);
    }, Math.max(quietWindow, energyWindow));
  }

  function csrfToken() {
    return document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
  }

  function updateQuestionBadge(label, state = "available") {
    const badge = $("#question-badge");
    if (!badge) return;
    badge.className = `service-badge overlay-service state-${state}`;
    badge.replaceChildren();
    const dot = document.createElement("i");
    badge.append(dot, document.createTextNode(label));
  }

  async function generateQuestions(force = false) {
    if (!isHost || !config.questionsUrl || stopping || leaving) return;
    if (!force && candidateRevision <= generatedRevision) return;
    if (questionInFlight) {
      if (!force) scheduleQuestionGeneration();
      return;
    }
    questionInFlight = true;
    const requestedRevision = candidateRevision;
    lastQuestionAt = Date.now();
    const button = $("#generate-questions");
    if (button) { button.disabled = true; button.classList.add("loading"); button.title = "Öneri hazırlanıyor"; }
    updateQuestionBadge("Yeni yanıt inceleniyor", "available");
    try {
      const url = force ? `${config.questionsUrl}?force=1` : config.questionsUrl;
      const response = await fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken(), "Accept": "application/json" },
        credentials: "same-origin",
        signal: AbortSignal.timeout(45000),
      });
      if (!response.ok) throw new Error("Soru servisi yanıt vermedi");
      const data = await response.json();
      if ((data.questions || []).length || !$("#question-list")?.querySelector(".question-card")) {
        renderQuestions(data.questions || []);
      }
      generatedRevision = Math.max(generatedRevision, requestedRevision);
      updateQuestionBadge(
        data.reused ? "Bu yanıt daha önce işlendi" : (data.provider === "rules" ? "Yerel kural önerisi" : "Qwen · yeni yanıt doğrulandı"),
        data.provider === "rules" ? "fallback" : "ready",
      );
    } catch (_) {
      updateQuestionBadge("Öneriler korunuyor · bağlantı tekrar denenecek", "error");
    } finally {
      questionInFlight = false;
      if (button) { button.disabled = false; button.classList.remove("loading"); button.title = "Yeni soru önerisi"; }
      if (candidateRevision > generatedRevision) scheduleQuestionGeneration();
    }
  }

  function renderQuestions(questions) {
    const list = $("#question-list");
    if (!list) return;
    if (!questions.length) {
      list.replaceChildren();
      const empty = document.createElement("p");
      empty.className = "question-empty";
      empty.textContent = "Yeni yanıt bekleniyor.";
      list.append(empty);
      if ($("#question-count")) $("#question-count").textContent = "0";
      return;
    }
    list.innerHTML = "";
    questions.forEach((question) => {
      const card = document.createElement("article");
      card.className = "question-card";
      card.dataset.questionId = question.id;
      const meta = document.createElement("div");
      meta.className = "question-meta";
      const type = document.createElement("span");
      type.textContent = question.type_label;
      const priority = document.createElement("small");
      priority.textContent = `Öncelik ${question.priority}`;
      meta.append(type, priority);
      const text = document.createElement("p");
      text.textContent = question.text;
      const actions = document.createElement("div");
      actions.className = "question-actions";
      actions.innerHTML = '<button data-action="dismissed">Geç</button><button class="ask-button" data-action="asked">Soruldu</button>';
      card.append(meta, text, actions);
      list.append(card);
    });
    const count = $("#question-count");
    if (count) count.textContent = questions.length;
  }

  async function setQuestionState(card, state) {
    const url = `/interviews/${config.interviewId}/questions/${card.dataset.questionId}/state/`;
    const response = await fetch(url, {
      method: "POST",
      headers: { "X-CSRFToken": csrfToken(), "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ state }),
    });
    if (response.ok) {
      card.remove();
      const count = $("#question-count");
      if (count) count.textContent = $("#question-list").querySelectorAll(".question-card").length;
    }
  }

  function stopMedia() {
    stopping = true;
    analysisEnabled = false;
    clearInterval(timerHandle);
    clearInterval(frameTimer);
    clearInterval(staleTimer);
    clearInterval(captureHealthTimer);
    clearTimeout(questionTimer);
    clearTimeout(captionTimer);
    clearTimeout(reconnectTimer);
    resetEmotions("Analiz kapalı");
    localStream?.getTracks().forEach((track) => track.stop());
    audioProcessor?.disconnect();
    if (audioContext && audioContext.state !== "closed") audioContext.close().catch(() => {});
    signalSocket?.close();
    asrSocket?.close();
    peer?.close();
    updateCaptureHealth();
  }

  async function leaveRoom() {
    if (leaving) return;
    if (isHost && !confirm("Mülakatı herkes için bitirmek istediğinize emin misiniz?")) return;
    leaving = true;
    leaveButton.disabled = true;
    leaveButton.querySelector("small").textContent = "Tamamlanıyor…";
    let peerFinished = Promise.resolve(true);
    if (isHost && peer?.connectionState === "connected") {
      peerFinished = new Promise((resolve) => {
        const timeout = setTimeout(() => { peerFlushResolve = null; resolve(false); }, 12000);
        peerFlushResolve = (ok) => { clearTimeout(timeout); peerFlushResolve = null; resolve(ok); };
        sendSignal({type: "prepare-close"});
      });
    }
    const results = await Promise.all([flushCapture(), peerFinished]);
    if (results.includes(false)) captionNotice("Son altyazının tamamı doğrulanamadı; görüşme sonlandırılıyor.");
    if (!isHost) {
      stopMedia();
      const workspace = document.querySelector(".video-workspace");
      workspace.innerHTML = '<div class="panel-empty"><span>✓</span><h3>Görüşmeden ayrıldınız</h3><p></p></div>';
      workspace.querySelector("p").textContent = results[0] ? "Bu pencereyi güvenle kapatabilirsiniz." : "Son altyazı tamamlanamamış olabilir. Bu pencereyi kapatabilirsiniz.";
      return;
    }
    try {
      const response = await fetch(config.completeUrl, {
        method: "POST", headers: {"X-CSRFToken": csrfToken(), "Accept": "application/json"},
        credentials: "same-origin", signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) throw new Error("Görüşme kapatılamadı");
      const data = await response.json();
      stopMedia();
      location.href = data.redirect;
    } catch (_) {
      leaving = false;
      leaveButton.disabled = false;
      leaveButton.querySelector("small").textContent = "Bitirmeyi tekrar dene";
      captionNotice("Ses/kamera durduruldu fakat görüşme sunucuda kapatılamadı. Bitirmeyi tekrar deneyin.");
    }
  }

  async function flushCapture() {
    stopping = true;
    analysisEnabled = false;
    clearInterval(frameTimer);
    clearTimeout(questionTimer);
    resetEmotions("Analiz kapalı");
    micButton.disabled = true;
    cameraButton.disabled = true;
    if (audioProcessor) {
      await new Promise((resolve) => {
        const timeout = setTimeout(() => { finishCaptureResolve = null; resolve(); }, 800);
        finishCaptureResolve = () => { clearTimeout(timeout); finishCaptureResolve = null; resolve(); };
        audioProcessor.port.postMessage({type: "finish"});
      });
    }
    localStream?.getTracks().forEach((track) => track.stop());
    audioProcessor?.disconnect();
    if (audioContext?.state !== "closed") await audioContext?.close();
    if (asrSocket?.readyState !== WebSocket.OPEN) return !localStream;
    return new Promise((resolve) => {
      const id = crypto.randomUUID();
      const timeout = setTimeout(() => { flushRequest = null; resolve(false); }, 10000);
      flushRequest = {id, resolve: (ok) => { clearTimeout(timeout); flushRequest = null; resolve(ok); }};
      asrSocket.send(JSON.stringify({type: "flush", request_id: id}));
    });
  }

  startButton?.addEventListener("click", startMedia);
  $("#toggle-captions")?.addEventListener("click", (event) => {
    const button = event.currentTarget;
    const enabled = button.getAttribute("aria-pressed") !== "true";
    button.setAttribute("aria-pressed", String(enabled));
    button.classList.toggle("off", !enabled);
    button.querySelector("small").textContent = enabled ? "Altyazı açık" : "Altyazı kapalı";
    const captions = $("#live-captions");
    if (captions) captions.hidden = !enabled;
  });
  micButton?.addEventListener("click", () => toggleTrack("audio", micButton));
  cameraButton?.addEventListener("click", () => toggleTrack("video", cameraButton));
  leaveButton?.addEventListener("click", leaveRoom);
  const copyInviteButton = $("#copy-invite");
  copyInviteButton?.addEventListener("click", async () => {
    await navigator.clipboard.writeText(config.joinUrl);
    const label = copyInviteButton.querySelector("small");
    if (label) label.textContent = "Kopyalandı";
    setTimeout(() => { if (label) label.textContent = "Davet"; }, 1500);
  });
  $("#generate-questions")?.addEventListener("click", () => generateQuestions(true));
  document.querySelectorAll("button[data-view-mode]").forEach((button) => {
    button.addEventListener("click", () => setViewMode(button.dataset.viewMode));
  });
  document.querySelectorAll("[data-focus-participant]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      setActiveParticipant(button.dataset.focusParticipant, true);
      setViewMode("speaker");
    });
  });
  videoGrid?.querySelectorAll(".video-tile").forEach((tile) => {
    tile.addEventListener("dblclick", () => {
      setActiveParticipant(tile.dataset.participant, true);
      setViewMode("speaker");
    });
  });
  const fullscreenButton = $("#toggle-fullscreen");
  if (fullscreenButton && (!document.fullscreenEnabled || !meetingStage?.requestFullscreen)) {
    fullscreenButton.hidden = true;
  } else {
    fullscreenButton?.addEventListener("click", async () => {
      try {
        if (document.fullscreenElement === meetingStage) await document.exitFullscreen();
        else await meetingStage.requestFullscreen();
      } catch (_) { captionNotice("Tam ekran görünümü bu tarayıcıda açılamadı."); }
    });
    document.addEventListener("fullscreenchange", syncFullscreenButton);
  }

  $("#question-list")?.addEventListener("click", (event) => {
    const actionButton = event.target.closest("[data-action]");
    const card = event.target.closest(".question-card");
    if (actionButton && card) setQuestionState(card, actionButton.dataset.action);
  });

  window.addEventListener("beforeunload", stopMedia);

  setViewMode(recall("view") || "gallery", false);

  function captionNotice(text) {
    const node = $("#live-captions");
    if (!node) return;
    node.replaceChildren();
    const line = document.createElement("p");
    line.className = "caption-waiting";
    line.textContent = text;
    node.append(line);
  }

  function showCaption(message) {
    const node = $("#live-captions");
    if (!node) return;
    captionHistory.push(message);
    while (captionHistory.length > 2) captionHistory.shift();
    node.replaceChildren();
    captionHistory.forEach((item) => {
      const line = document.createElement("p");
      const speaker = document.createElement("b");
      speaker.textContent = item.speaker_label;
      line.append(speaker, document.createTextNode(item.text));
      node.append(line);
    });
    clearTimeout(captionTimer);
    captionTimer = setTimeout(() => { captionHistory.length = 0; captionNotice("Konuşma bekleniyor…"); }, 16000);
  }

  function resetEmotions(label) {
    document.querySelectorAll(".emotion-card").forEach((card) => {
      card.querySelector(".emotion-label").textContent = label;
      card.querySelector(".emotion-scores").replaceChildren();
      card.querySelector(".emotion-detail").textContent = "Sonuçlar yalnız size gösterilir ve kaydedilmez.";
      card.querySelector(".emotion-latency").textContent = "";
      card.classList.remove("is-stale");
      delete card.dataset.updatedAt;
    });
  }

  function renderEmotion(message) {
    if (!["text", "audio", "video"].includes(message.modality)) return;
    const card = document.querySelector(`[data-modality="${message.modality}"]`);
    if (!card) return;
    const scores = message.scores || [];
    const success = ["ok", "uncertain"].includes(message.state) && scores.length > 0;
    const recent = card.dataset.updatedAt && Date.now() - Number(card.dataset.updatedAt) < 20000;
    const signal = message.signal ? ` Sunucu: ${(message.signal.duration_ms / 1000).toFixed(1)} sn ses · ${message.signal.rms_dbfs} dBFS · aktif seviye ${message.signal.active_ms} ms.` : "";
    // A silent window is not a replacement emotion. Keep the previous result
    // briefly, explicitly as historical; never refresh its expiry timestamp.
    if (!success && recent && ["no_data", "busy"].includes(message.state) && message.detail !== "Kamera kapalı.") {
      card.querySelector(".emotion-label").textContent = `Son tahmin · ${card.dataset.resultLabel}`;
      card.querySelector(".emotion-detail").textContent = `Yeni sonuç yok: ${message.detail || "Veri bekleniyor."}${signal}`;
      card.querySelector(".emotion-latency").textContent = `${Math.floor((Date.now() - Number(card.dataset.updatedAt)) / 1000)} sn önceki pencere · güncel ölçüm değil`;
      return;
    }
    card.classList.remove("is-stale");
    if (success) {
      card.querySelector(".model-state").textContent = "Hazır";
      card.dataset.updatedAt = Date.now();
    } else { delete card.dataset.updatedAt; }
    const states = {no_data: "Veri yok", error: "Model hatası", busy: "Model meşgul", disabled: "Analiz kapalı", uncertain: "Belirsiz"};
    card.querySelector(".emotion-label").textContent = states[message.state] || scores[0]?.label_tr || "Veri bekleniyor";
    if (success) card.dataset.resultLabel = card.querySelector(".emotion-label").textContent;
    const list = card.querySelector(".emotion-scores");
    list.replaceChildren();
    scores.slice(0, 1).forEach((score) => {
      const row = document.createElement("div");
      row.className = "emotion-score";
      const label = document.createElement("span");
      label.textContent = score.label_tr;
      const value = document.createElement("span");
      value.textContent = `%${Math.round(score.score * 100)} skor`;
      const progress = document.createElement("progress");
      progress.max = 1; progress.value = score.score;
      progress.setAttribute("aria-label", score.label_tr);
      row.append(label, value, progress); list.append(row);
    });
    card.querySelector(".emotion-detail").textContent = (message.detail || "") + signal;
    card.querySelector(".emotion-latency").textContent = message.latency_ms ? `İşleme: ${message.latency_ms} ms · yalnız bu pencere` : "";
  }

  async function sendFrame() {
    if (!analysisEnabled || frameInFlight || asrSocket?.readyState !== WebSocket.OPEN || asrSocket.bufferedAmount > 96000) return;
    if (!localStream?.getVideoTracks()[0]?.enabled || !localVideo.videoWidth) return;
    frameInFlight = true;
    const width = Math.min(640, localVideo.videoWidth);
    const scale = Math.min(width / localVideo.videoWidth, 480 / localVideo.videoHeight);
    frameCanvas.width = Math.round(localVideo.videoWidth * scale);
    frameCanvas.height = Math.round(localVideo.videoHeight * scale);
    frameCanvas.getContext("2d").drawImage(localVideo, 0, 0, frameCanvas.width, frameCanvas.height);
    frameCanvas.toBlob(async (blob) => {
      try {
        if (!blob || blob.size > 180000 || !analysisEnabled) return;
        const data = new Uint8Array(await blob.arrayBuffer());
        let binary = "";
        for (const byte of data) binary += String.fromCharCode(byte);
        if (analysisEnabled && asrSocket?.readyState === WebSocket.OPEN) asrSocket.send(JSON.stringify({type: "frame", jpeg: btoa(binary)}));
      } finally { frameInFlight = false; }
    }, "image/jpeg", .72);
  }
})();
