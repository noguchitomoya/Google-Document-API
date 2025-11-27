"use strict";

(function () {
  const bootstrap = window.APP_BOOTSTRAP || {
    teachers: [],
    students: [],
    driveTargets: [],
  };

  const setupForm = document.getElementById("setup-form");
  const entryForm = document.getElementById("entry-form");
  const teacherSelect = document.getElementById("teacher-select");
  const studentSelect = document.getElementById("student-select");
  const driveManual = document.getElementById("drive-manual");
  const copyPreviousCheckbox = document.getElementById("copy-previous");
  const entrySection = document.getElementById("entry-section");
  const stepSection = document.getElementById("step-picker");
  const backButton = document.getElementById("back-button");
  const autosaveStatus = document.querySelector("[data-autosave-status]");
  const entrySummary = document.querySelector("[data-entry-summary]");
  const setupErrorBox = document.querySelector("[data-error-box]");
  const entryMessageBox = document.querySelector("[data-entry-message]");

  const MODE = { EXISTING: "existing", NEW: "new" };
  const AUTOSAVE_DELAY = 1200;

  const state = {
    mode: MODE.EXISTING,
    copyPrevious: false,
    context: null,
    teacherId: "",
    teacherName: "",
    studentId: "",
    studentName: "",
    driveParentId: "",
    driveLabel: "",
    listenersBound: false,
    autosaveTimer: null,
    lastPayload: null,
  };

  function getSelectedOption(select) {
    if (!select) return null;
    const index = select.selectedIndex;
    if (typeof index !== "number" || index < 0) {
      return null;
    }
    return select.options[index] || null;
  }

  function init() {
    populateSelect(teacherSelect, bootstrap.teachers, (teacher) => ({
      value: teacher.id,
      label: teacher.subject
        ? `${teacher.name}（${teacher.subject}）`
        : teacher.name,
    }));
    populateSelect(studentSelect, bootstrap.students, (student) => ({
      value: student.id,
      label: `${student.name}（${student.grade || "学年未設定"}）`,
    }));
    bindTabs();
    bindSetupForm();
    bindEntryForm();
    bindBackButton();
  }

  function populateSelect(select, items, mapper) {
    if (!select) return;
    items.forEach((item) => {
      const mapped = mapper(item);
      const option = document.createElement("option");
      option.value = mapped.value;
      option.textContent = mapped.label;
      if (mapped.dataset) {
        Object.entries(mapped.dataset).forEach(([key, value]) => {
          option.dataset[key] = value;
        });
      }
      select.appendChild(option);
    });
  }

  function bindTabs() {
    const tabs = stepSection.querySelectorAll(".tab");
    const panes = stepSection.querySelectorAll(".tab-pane");
    tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        tabs.forEach((t) => {
          t.classList.remove("is-active");
          t.setAttribute("aria-selected", "false");
        });
        tab.classList.add("is-active");
        tab.setAttribute("aria-selected", "true");
        state.mode = tab.dataset.mode === MODE.NEW ? MODE.NEW : MODE.EXISTING;
        panes.forEach((pane) => {
          pane.classList.toggle("is-active", pane.dataset.pane === state.mode);
        });
        clearError();
      });
    });
  }

  function bindSetupForm() {
    if (!setupForm) return;
    setupForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearError();

      const teacherId = teacherSelect.value;
      if (!teacherId) {
        return showError("講師を選択してください。");
      }
      const teacher = bootstrap.teachers.find((t) => t.id === teacherId);

      let studentId = "";
      let studentName = "";
      let newStudentGrade = "";
      let newStudentMemo = "";

      if (state.mode === MODE.EXISTING) {
        studentId = studentSelect.value;
        if (!studentId) {
          return showError("生徒を選択してください。");
        }
        const selectedStudent = getSelectedOption(studentSelect);
        studentName = selectedStudent
          ? selectedStudent.textContent.split("（")[0].trim()
          : "";
      } else {
        studentName = document
          .getElementById("new-student-name")
          .value.trim();
        newStudentGrade = document
          .getElementById("new-student-grade")
          .value.trim();
        newStudentMemo = document
          .getElementById("new-student-memo")
          .value.trim();
        if (!studentName) {
          return showError("新規生徒の名前を入力してください。");
        }
      }

      const driveParentId = deriveDriveFolderId();
      const driveLabel = deriveDriveFolderLabel(driveParentId) || "未指定（デフォルト）";

      state.copyPrevious = copyPreviousCheckbox.checked;
      state.teacherId = teacherId;
      state.teacherName = teacher ? teacher.name : "";
      state.studentId = studentId;
      state.studentName = studentName;
      state.driveParentId = driveParentId;
      state.driveLabel = driveLabel;

      const params = new URLSearchParams({
        teacherId,
        mode: state.mode,
        copyPrevious: String(state.copyPrevious),
      });
      if (state.mode === MODE.EXISTING) {
        params.append("studentId", studentId);
      } else {
        params.append("studentName", studentName);
      }

      try {
        setAutosaveStatus("info", "準備中...");
        const response = await fetch(`/api/context?${params.toString()}`);
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.error || "下準備に失敗しました。");
        }
        openEntrySection({
          context: data,
          teacher,
          newStudentGrade,
          newStudentMemo,
          driveParentId,
          driveLabel,
        });
      } catch (error) {
        showError(error.message);
        setAutosaveStatus("error", "準備に失敗しました");
      }
    });
  }

  function deriveDriveFolderId() {
    const manual = driveManual.value.trim();
    if (manual) {
      return extractDriveFolderId(manual);
    }
    return "";
  }

  function deriveDriveFolderLabel(folderId) {
    const manual = driveManual.value.trim();
    if (manual) {
      return manual;
    }
    if (folderId) {
      return folderId;
    }
    return "";
  }

  function extractDriveFolderId(value) {
    if (!value) return "";
    const folderMatch = value.match(/\/folders\/([a-zA-Z0-9_-]+)/);
    if (folderMatch) {
      return folderMatch[1];
    }
    const idMatch = value.match(/id=([a-zA-Z0-9_-]+)/);
    if (idMatch) {
      return idMatch[1];
    }
    if (/^[a-zA-Z0-9_-]+$/.test(value)) {
      return value;
    }
    return "";
  }

  function openEntrySection({ context, teacher, newStudentGrade, newStudentMemo }) {
    state.context = context;

    entryForm.elements.teacher_id.value = state.teacherId;
    entryForm.elements.student_mode.value = state.mode;
    entryForm.elements.drive_parent_id.value = state.driveParentId;
    entryForm.elements.copy_previous.value = state.copyPrevious ? "on" : "off";
    entryForm.elements.student_key.value = context.studentKey;
    entryForm.elements.student_identifier.value = context.studentIdentifier;

    if (state.mode === MODE.EXISTING) {
      entryForm.elements.student_id.value = state.studentId;
      entryForm.elements.new_student_name.value = "";
      entryForm.elements.new_student_grade.value = "";
      entryForm.elements.new_student_memo.value = "";
    } else {
      entryForm.elements.student_id.value = "";
      entryForm.elements.new_student_name.value = state.studentName;
      entryForm.elements.new_student_grade.value = newStudentGrade;
      entryForm.elements.new_student_memo.value = newStudentMemo;
    }

    const teacherName = teacher ? teacher.name : "";
    entrySummary.textContent = `${teacherName} → ${state.studentName} ／ 保存先: ${state.driveLabel}`;

    if (state.copyPrevious && context.previous) {
      setEntryMessage(
        "前回の記録を読み込みました。必要に応じて追記・修正してください。",
        "info"
      );
    } else if (state.copyPrevious) {
      setEntryMessage("前回の記録は見つかりませんでした。新規テンプレートを表示します。", "info");
    } else {
      setEntryMessage("", "");
    }

    fillFormFields(resolveInitialPayload(context));

    entrySection.classList.remove("is-hidden");
    entrySection.scrollIntoView({ behavior: "smooth" });
    if (!state.listenersBound) {
      bindAutosaveListeners();
      state.listenersBound = true;
    }
    setAutosaveStatus("info", "編集を開始しました");
  }

  function resolveInitialPayload(context) {
    const base = { ...(context.templateFields || {}) };
    if (
      state.copyPrevious &&
      context.previous &&
      context.previous.payload
    ) {
      Object.assign(base, context.previous.payload);
    }
    if (context.draft && context.draft.payload) {
      Object.assign(base, context.draft.payload);
    }
    return base;
  }

  function fillFormFields(payload) {
    const mapping = {
      lesson_date: "lesson_date",
      lesson_goal: "lesson_goal",
      lesson_summary: "lesson_summary",
      student_reaction: "student_reaction",
      next_actions: "next_actions",
      memo: "memo",
    };
    Object.entries(mapping).forEach(([field, name]) => {
      if (entryForm.elements[name]) {
        entryForm.elements[name].value = payload[field] || "";
      }
    });
  }

  function bindAutosaveListeners() {
    const inputs = entryForm.querySelectorAll(
      "input[type='text'], input[type='date'], textarea"
    );
    inputs.forEach((input) => {
      input.addEventListener("input", handleFieldChange);
    });
  }

  function handleFieldChange() {
    setAutosaveStatus("saving", "自動保存を準備しています…");
    if (state.autosaveTimer) {
      clearTimeout(state.autosaveTimer);
    }
    state.autosaveTimer = window.setTimeout(runAutosave, AUTOSAVE_DELAY);
  }

  async function runAutosave() {
    if (!state.context || !state.context.studentKey) return;
    const payload = collectPayload();
    if (JSON.stringify(payload) === JSON.stringify(state.lastPayload)) {
      setAutosaveStatus("saved", "最新の状態です");
      return;
    }
    setAutosaveStatus("saving", "自動保存中…");
    try {
      const response = await fetch("/api/drafts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          studentKey: state.context.studentKey,
          payload,
        }),
      });
      if (!response.ok) {
        throw new Error("autosave failed");
      }
      state.lastPayload = payload;
      setAutosaveStatus(
        "saved",
        `自動保存済み ${new Date().toLocaleTimeString()}`
      );
    } catch (error) {
      console.error(error);
      setAutosaveStatus("error", "自動保存に失敗しました");
    }
  }

  function collectPayload() {
    return {
      lesson_date: entryForm.elements.lesson_date.value,
      lesson_goal: entryForm.elements.lesson_goal.value,
      lesson_summary: entryForm.elements.lesson_summary.value,
      student_reaction: entryForm.elements.student_reaction.value,
      next_actions: entryForm.elements.next_actions.value,
      memo: entryForm.elements.memo.value,
      student_name: state.studentName,
      teacher_name: state.teacherName,
    };
  }

  function setAutosaveStatus(stateName, text) {
    if (!autosaveStatus) return;
    autosaveStatus.dataset.state = stateName;
    autosaveStatus.textContent = text;
  }

  function showError(message) {
    if (!setupErrorBox) return;
    setupErrorBox.textContent = message;
    setupErrorBox.classList.remove("is-hidden");
  }

  function clearError() {
    if (!setupErrorBox) return;
    setupErrorBox.textContent = "";
    setupErrorBox.classList.add("is-hidden");
  }

  function setEntryMessage(message, variant) {
    if (!entryMessageBox) return;
    if (!message) {
      entryMessageBox.classList.add("is-hidden");
      return;
    }
    entryMessageBox.textContent = message;
    entryMessageBox.dataset.variant = variant || "info";
    entryMessageBox.classList.remove("is-hidden");
  }

  function bindEntryForm() {
    if (!entryForm) return;
    entryForm.addEventListener("submit", () => {
      if (state.autosaveTimer) {
        clearTimeout(state.autosaveTimer);
      }
      setAutosaveStatus("info", "登録処理を開始します…");
    });
  }

  function bindBackButton() {
    if (!backButton) return;
    backButton.addEventListener("click", () => {
      entrySection.classList.add("is-hidden");
      stepSection.scrollIntoView({ behavior: "smooth" });
    });
  }

  init();
})();
