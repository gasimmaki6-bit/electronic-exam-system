/**
 * anticheat.js
 * نظام منع الغش لصفحة أداء الامتحان:
 * - كشف الخروج من التبويب / تصغير النافذة (visibilitychange, blur)
 * - إجبار وضع ملء الشاشة
 * - تعطيل النقر بزر الفأرة الأيمن والنسخ واللصق واختصارات لوحة المفاتيح
 * - عداد تنازلي للوقت مع تسليم تلقائي عند انتهائه
 * - تسليم تلقائي بعد تجاوز عدد مخالفات معين
 * جميع النصوص المعروضة للمستخدم تُمرَّر عبر config.i18n لدعم تعدد اللغات.
 */

function initAntiCheat(config) {
    const submissionId = config.submissionId;
    const violationUrl = config.violationUrl;
    let remainingSeconds = config.remainingSeconds;
    const i18n = config.i18n || {};
    const examForm = document.getElementById("exam-form");
    const timerEl = document.getElementById("timer-display");
    const violationBox = document.getElementById("violation-box");
    const violationText = document.getElementById("violation-text");

    let isSubmitting = false;

    function submitExam(auto) {
        if (isSubmitting) return;
        isSubmitting = true;
        document.getElementById("auto_submitted_input").value = auto ? "1" : "0";
        examForm.submit();
    }

    // ---------- العداد التنازلي ----------
    function tick() {
        if (isSubmitting) return;
        if (remainingSeconds <= 0) {
            alert(i18n.timeUpAlert || "Time is up.");
            submitExam(true);
            return;
        }
        const m = Math.floor(remainingSeconds / 60).toString().padStart(2, "0");
        const s = Math.floor(remainingSeconds % 60).toString().padStart(2, "0");
        timerEl.textContent = `${i18n.timeRemaining || "Time remaining"}: ${m}:${s}`;
        timerEl.classList.toggle("timer-critical", remainingSeconds <= 120);
        remainingSeconds -= 1;
    }
    tick();
    setInterval(tick, 1000);

    // ---------- الإبلاغ عن مخالفة ----------
    function reportViolation(reasonKey) {
        if (isSubmitting) return;
        fetch(violationUrl, {
            method: "POST",
            headers: { "X-Requested-With": "XMLHttpRequest" },
        })
            .then((r) => r.json())
            .then((data) => {
                if (data.status !== "ok") return;
                const reasonText = i18n[reasonKey] || reasonKey;
                violationBox.style.display = "block";
                violationText.textContent =
                    `${i18n.violationPrefix || "Warning"} (${reasonText}). ` +
                    `${i18n.violationCountLabel || "Violations"}: ${data.violation_count} ${i18n.violationOf || "of"} ${data.max_violations}.`;
                if (data.force_submit) {
                    alert(i18n.forceSubmitAlert || "Too many violations, submitting now.");
                    submitExam(true);
                }
            })
            .catch(() => {});
    }

    // كشف تبديل التبويب / تصغير النافذة
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) {
            reportViolation("violationTab");
        }
    });

    window.addEventListener("blur", () => {
        reportViolation("violationBlur");
    });

    // ---------- إجبار وضع ملء الشاشة ----------
    function enterFullscreen() {
        const el = document.documentElement;
        if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
        else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
    }

    document.addEventListener("fullscreenchange", () => {
        if (document.fullscreenElement) {
            // دخول ملء الشاشة (تلقائياً أو بالنقر) -> أزل الطبقة الحاجزة إن كانت ظاهرة
            const gate = document.getElementById("fullscreen-gate");
            if (gate) gate.style.display = "none";
        } else if (!isSubmitting) {
            reportViolation("violationFullscreen");
        }
    });

    const gate = document.getElementById("fullscreen-gate");
    const startBtn = document.getElementById("start-fullscreen-btn");

    if (startBtn) {
        startBtn.addEventListener("click", () => {
            enterFullscreen();
            if (gate) gate.style.display = "none";
        });
    }

    // محاولة تلقائية فور تحميل الصفحة: تعمل في بعض المتصفحات/السياقات، وتفشل بصمت
    // في أغلبها بسبب قيد أمني يمنع تفعيل ملء الشاشة دون نقرة مباشرة من المستخدم —
    // لذا تبقى الطبقة الحاجزة والزر أعلاه هما الضمانة الفعلية.
    window.addEventListener("load", () => {
        enterFullscreen();
    });

    // ---------- تعطيل النسخ / اللصق / القص / الزر الأيمن ----------
    document.addEventListener("contextmenu", (e) => e.preventDefault());
    document.addEventListener("copy", (e) => e.preventDefault());
    document.addEventListener("paste", (e) => e.preventDefault());
    document.addEventListener("cut", (e) => e.preventDefault());

    document.addEventListener("keydown", (e) => {
        const blockedKeys = ["F12"];
        const isDevTools =
            (e.ctrlKey && e.shiftKey && ["I", "J", "C"].includes(e.key.toUpperCase())) ||
            (e.ctrlKey && e.key.toUpperCase() === "U");
        if (blockedKeys.includes(e.key) || isDevTools) {
            e.preventDefault();
            reportViolation("violationDevtools");
        }
    });

    window.addEventListener("beforeunload", (e) => {
        if (!isSubmitting) {
            e.preventDefault();
            e.returnValue = "";
        }
    });

    const manualSubmitBtn = document.getElementById("manual-submit-btn");
    if (manualSubmitBtn) {
        manualSubmitBtn.addEventListener("click", (e) => {
            e.preventDefault();
            if (confirm(i18n.confirmSubmit || "Submit the exam?")) {
                submitExam(false);
            }
        });
    }
}
