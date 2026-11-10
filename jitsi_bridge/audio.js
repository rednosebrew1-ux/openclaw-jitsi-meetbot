// Injected into the Jitsi Meet page before any of its scripts run.
//
// - The microphone: getUserMedia returns the AI's voice (PCM pushed in by the bridge) instead
//   of a real mic.
// - The meeting audio: every remote audio track the page receives is mixed and handed to the
//   bridge as 32 kHz 16-bit mono PCM (base64) through window.__aiAudioOut.
// - window.__ai.state() reports whether we're in the conference and how many people are there.
(() => {
    if (window.__ai) {
        return;
    }
    const RATE = 32000;
    const BLOCK = 1024; // samples per ScriptProcessor callback (32 ms)
    const MAX_QUEUED = RATE; // drop the oldest voice audio beyond 1 s (the bridge sends in real time)
    const ai = {};

    window.__ai = ai;

    let ctx = null;
    let queue = []; // Float32Array chunks of the AI's voice waiting to be played
    let queued = 0;

    function toBase64(int16) {
        const bytes = new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength);
        let binary = '';

        for (let i = 0; i < bytes.length; i += 0x8000) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
        }

        return btoa(binary);
    }

    function fromBase64(b64) {
        const binary = atob(b64);
        const bytes = new Uint8Array(binary.length);

        for (let i = 0; i < binary.length; i++) {
            bytes[i] = binary.charCodeAt(i);
        }
        const int16 = new Int16Array(bytes.buffer, 0, bytes.length >> 1);
        const out = new Float32Array(int16.length);

        for (let i = 0; i < int16.length; i++) {
            out[i] = int16[i] / 32768;
        }

        return out;
    }

    function context() {
        if (ctx) {
            if (ctx.state === 'suspended') {
                ctx.resume().catch(() => {});
            }

            return ctx;
        }
        ctx = new AudioContext({ sampleRate: RATE });

        // The AI's voice -> a MediaStream the page will use as its microphone.
        const speaker = ctx.createScriptProcessor(BLOCK, 1, 1);
        const silence = ctx.createConstantSource();

        silence.offset.value = 0;
        silence.connect(speaker);
        silence.start();
        speaker.onaudioprocess = e => {
            const out = e.outputBuffer.getChannelData(0);
            let i = 0;

            while (i < out.length && queue.length) {
                const chunk = queue[0];
                const n = Math.min(chunk.length, out.length - i);

                out.set(chunk.subarray(0, n), i);
                i += n;
                queued -= n;
                if (n === chunk.length) {
                    queue.shift();
                } else {
                    queue[0] = chunk.subarray(n);
                }
            }
            out.fill(0, i);
        };
        ai.voice = ctx.createMediaStreamDestination();
        speaker.connect(ai.voice);

        // Everyone else -> mixed -> the bridge.
        ai.mix = ctx.createGain();
        const capture = ctx.createScriptProcessor(BLOCK, 1, 1);
        const mute = ctx.createGain();

        mute.gain.value = 0; // keeps the capture node running without playing anything
        ai.mix.connect(capture);
        capture.connect(mute);
        mute.connect(ctx.destination);
        capture.onaudioprocess = e => {
            if (!window.__aiAudioOut || !ai.remoteTracks) {
                return;
            }
            const input = e.inputBuffer.getChannelData(0);
            const int16 = new Int16Array(input.length);

            for (let i = 0; i < input.length; i++) {
                const s = Math.max(-1, Math.min(1, input[i]));

                int16[i] = s < 0 ? s * 32768 : s * 32767;
            }
            window.__aiAudioOut(toBase64(int16));
        };

        return ctx;
    }

    // Called by the bridge with 32 kHz 16-bit mono PCM (base64).
    ai.play = b64 => {
        context();
        const samples = fromBase64(b64);

        queue.push(samples);
        queued += samples.length;
        while (queued > MAX_QUEUED && queue.length > 1) {
            queued -= queue.shift().length;
        }
    };

    ai.clear = () => {
        queue = [];
        queued = 0;
    };

    ai.remoteTracks = 0;
    ai.addRemote = track => {
        const c = context();
        const stream = new MediaStream([ track ]);

        // Chrome only feeds remote WebRTC audio into Web Audio while a media element plays it too.
        const el = new Audio();

        el.muted = true;
        el.srcObject = stream;
        el.play().catch(() => {});
        const source = c.createMediaStreamSource(stream);

        source.connect(ai.mix);
        ai.remoteTracks++;
        track.addEventListener('ended', () => {
            source.disconnect();
            ai.remoteTracks--;
        });
    };

    // Hear every remote audio track, whichever connection it arrives on.
    const OriginalPC = window.RTCPeerConnection;

    if (OriginalPC) {
        class BotPeerConnection extends OriginalPC {
            constructor(...args) {
                super(...args);
                this.addEventListener('track', e => {
                    if (e.track.kind === 'audio') {
                        ai.addRemote(e.track);
                    }
                });
            }
        }
        window.RTCPeerConnection = BotPeerConnection;
        window.webkitRTCPeerConnection = BotPeerConnection;
    }

    // The microphone is the AI's voice.
    const devices = navigator.mediaDevices;

    if (devices && devices.getUserMedia) {
        const originalGUM = devices.getUserMedia.bind(devices);

        devices.getUserMedia = async constraints => {
            if (!constraints || !constraints.audio) {
                return originalGUM(constraints);
            }
            context();
            const tracks = [ ai.voice.stream.getAudioTracks()[0].clone() ];

            if (constraints.video) {
                try {
                    tracks.push(...(await originalGUM({ video: constraints.video })).getVideoTracks());
                } catch (e) {
                    // audio only is fine
                }
            }

            return new MediaStream(tracks);
        };
    }

    // What the bridge polls.
    ai.state = () => {
        const conference = window.APP && window.APP.conference;
        let joined = false;
        let members = 0;
        let muted = true;

        try {
            joined = Boolean(conference && conference.isJoined());
            members = joined ? conference.membersCount : 0;
            muted = conference ? conference.isLocalAudioMuted() : true;
        } catch (e) {
            // the app isn't ready yet
        }

        return { joined, members, muted };
    };
})();
