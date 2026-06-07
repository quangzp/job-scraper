(function () {
  'use strict';

  var submitSelector = 'input[type="submit"], button[type="submit"], button:not([type])';
  var loadingClass = 'dashboard-submit-loading';
  var disabledClass = 'dashboard-submit-disabled';
  var spinnerClass = 'dashboard-submit-spinner';

  function getSubmitButtons(form) {
    return Array.prototype.slice.call(form.querySelectorAll(submitSelector));
  }

  function rememberSubmitter(event) {
    if (!event.target.closest) {
      return;
    }

    var button = event.target.closest(submitSelector);
    if (!button || !button.form) {
      return;
    }
    button.form.dashboardSubmitter = button;
  }

  function addSubmitterFallback(form, submitter) {
    if (!submitter || !submitter.name) {
      return;
    }

    var hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.name = submitter.name;
    hidden.value = submitter.value || '';
    hidden.dataset.dashboardSubmitFallback = 'true';
    form.appendChild(hidden);
  }

  function ensureDeleteActionInput(form) {
    var actionInput = form.querySelector('input[name="action"]');
    if (actionInput) {
      actionInput.value = 'delete_selected';
      return;
    }

    actionInput = document.createElement('input');
    actionInput.type = 'hidden';
    actionInput.name = 'action';
    actionInput.value = 'delete_selected';
    actionInput.dataset.dashboardDeleteFallback = 'true';
    form.appendChild(actionInput);
  }

  function addInputSpinner(form, submitter) {
    if (!submitter || submitter.tagName.toLowerCase() !== 'input') {
      return;
    }

    var rect = submitter.getBoundingClientRect();
    var spinner = document.createElement('span');
    spinner.className = spinnerClass;
    spinner.dataset.dashboardSubmitSpinner = 'true';
    spinner.style.color = window.getComputedStyle(submitter).color;
    spinner.style.height = '1em';
    spinner.style.left = (rect.left + 12) + 'px';
    spinner.style.top = (rect.top + (rect.height / 2) - 8) + 'px';
    spinner.style.width = '1em';
    submitter.dataset.dashboardOriginalValue = submitter.value || '';
    submitter.value = 'Saving...';
    document.body.appendChild(spinner);
  }

  function setLoadingState(form, submitter) {
    addInputSpinner(form, submitter);

    getSubmitButtons(form).forEach(function (button) {
      button.disabled = true;
      button.classList.add(disabledClass);
    });

    if (submitter) {
      submitter.classList.add(loadingClass);
      submitter.setAttribute('aria-busy', 'true');
    }
  }

  function isDeleteSubmitter(submitter) {
    return Boolean(submitter && submitter.dataset && submitter.dataset.dashboardDeleteAction === 'true');
  }

  function selectedActionCount(form) {
    return form.querySelectorAll('input[name="_selected_action"]:checked').length;
  }

  function showDeleteSelectionError(form) {
    var error = form.querySelector('.dashboard-delete-error');
    if (!error) {
      return;
    }

    error.hidden = false;
    error.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  function clearDeleteSelectionError(form) {
    var error = form.querySelector('.dashboard-delete-error');
    if (error) {
      error.hidden = true;
    }
  }

  document.addEventListener('click', rememberSubmitter, true);

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') {
      return;
    }

    if (form.dataset.dashboardSubmitting === 'true') {
      event.preventDefault();
      return;
    }

    var submitter = event.submitter || form.dashboardSubmitter;
    if (isDeleteSubmitter(submitter) && selectedActionCount(form) === 0) {
      event.preventDefault();
      clearDeleteSelectionError(form);
      showDeleteSelectionError(form);
      return;
    }

    clearDeleteSelectionError(form);
    if (isDeleteSubmitter(submitter)) {
      ensureDeleteActionInput(form);
    }

    form.dataset.dashboardSubmitting = 'true';
    addSubmitterFallback(form, submitter);
    setLoadingState(form, submitter);
  }, true);

  window.addEventListener('pageshow', function () {
    document.querySelectorAll('form[data-dashboard-submitting="true"]').forEach(function (form) {
      delete form.dataset.dashboardSubmitting;
      delete form.dashboardSubmitter;

      form.querySelectorAll('[data-dashboard-submit-fallback="true"]').forEach(function (input) {
        input.remove();
      });

      form.querySelectorAll('[data-dashboard-delete-fallback="true"]').forEach(function (input) {
        input.remove();
      });

      document.querySelectorAll('[data-dashboard-submit-spinner="true"]').forEach(function (spinner) {
        spinner.remove();
      });

      getSubmitButtons(form).forEach(function (button) {
        button.disabled = false;
        button.classList.remove(disabledClass, loadingClass);
        button.removeAttribute('aria-busy');
        if (button.dataset.dashboardOriginalValue !== undefined) {
          button.value = button.dataset.dashboardOriginalValue;
          delete button.dataset.dashboardOriginalValue;
        }
      });
    });
  });
})();
