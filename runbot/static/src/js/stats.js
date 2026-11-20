const aggregateKeys = { sum: 'Aggregate Sum', average: 'Aggregate Average' };
let statsRequest;

var config = {
  type: 'line',
  options: {

    animation: {
      duration: 0
    },
    plugins: {
      legend: {
        display: false
      },
      tooltip: {
        mode: 'point',
      },
    },
    responsive: true,
    scales: {
      x: {
        display: true,
        title: {
          display: true,
          text: 'Builds',
        }
      },
      y: {
        display: true,
        title: {
          display: true,
          text: 'Value',
        },
      },
    },
  },
};

config.options.onClick = function(event, activeElements) {
    if (activeElements.length === 0){
      return
    }
    const build_id = config.data.builds[activeElements[0].index];
    if (event.native.shiftKey){
      config.searchParams['center_build_id'] = build_id;
      fetchUpdateChart();
    } else {
      window.open('/runbot/build/' + build_id);
    }

};

async function fetchStats(path, data, signal) {
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ params: data }),
      signal,
    });
    return (await response.json()).result;
  } catch {
    // failed or aborted requests have no result
  }
}

function random_color(name){
    var colors = ['#004acd', '#3658c3', '#4a66ba', '#5974b2', '#6581aa', '#6f8fa3', '#7a9c9d', '#85a899', '#91b596', '#a0c096', '#fdaf56', '#f89a59', '#f1865a', '#e87359', '#dc6158', '#ce5055', '#bf4150', '#ad344b', '#992a45', '#84243d'];
    var sum = 0;
    for (var i = 0; i < name.length; i++) {
        sum += name.charCodeAt(i);
    }
    sum = sum % colors.length;
    color = colors[sum];

    return color
};


function process_chart_data(){
    if (! config.result || Object.keys(config.result).length == 0)
    {
      config.data = {
        labels:[],
        datasets: [],
      }
      return
    }

    var aggregate = document.getElementById('display_aggregate_selector').value;
    var aggregates = {};


    const builds = config.builds;
    var older_build_stats = config.result[builds[0]];
    // keys of all the builds, some can miss in the newer one
    const keys = [...new Set(builds.flatMap((build) => Object.keys(config.result[build])))];
    function lastValue(key) {
      return builds.map((build) => config.result[build][key]).findLast((value) => value !== undefined);
    }
    if (aggregate != 'sum') {
      keys.splice(keys.indexOf('Aggregate Sum'), 1);
    }
    if (aggregate != 'average') {
      keys.splice(keys.indexOf('Aggregate Average'), 1);
    }
    var mode = document.getElementById('mode_selector').value;

    var sort_values = {}
    for (key of keys) {
      sort_value = NaN
      if (mode == 'normal') {
        sort_value = lastValue(key);
      } else if (mode == 'change_count') {
        sort_value = 0
        previous = undefined
        for (build of builds) {
          res = config.result[build]
          value = res[key]
          if (previous !== undefined && value !== undefined && previous != value) {
            sort_value +=1
          }
          previous = value
        }
      }
      else {
        if (mode == "difference") {
          var previous_value = 0;
          if (older_build_stats[key] !== undefined) {
            previous_value = older_build_stats[key]
          }
          sort_value = Math.abs(lastValue(key) - previous_value);
        }
      }
      sort_values[key] = sort_value
    }
    if (mode == 'alpha') {
      keys.sort((m1, m2) => m1.localeCompare(m2));
    } else {
      keys.sort((m1, m2) => sort_values[m2] - sort_values[m1]);
    }

    if (config.searchParams.nb_dataset != -1) {
      visible_keys = new Set(keys.slice(0, config.searchParams.nb_dataset));
      // the chosen aggregate is shown, even when not in the top values
      if (aggregate != 'none') {
        visible_keys.add(aggregateKeys[aggregate]);
      }
    } else {
      // keys are encoded, as they can contain the separator
      visible_keys = new Set(config.searchParams.visible_keys.split(',').filter(Boolean).map(decodeURIComponent));
    }
    console.log(visible_keys);
    function display_value(key, build_stats){
      if (build_stats[key] === undefined)
          return NaN;
      if (mode == 'normal' || mode == 'alpha')
          return build_stats[key]
      var previous_value = 0;
      if (older_build_stats[key] !== undefined) {
            previous_value = older_build_stats[key]
      }
      return  build_stats[key] - previous_value
    }

    config.data = {
      builds: builds,
      labels: builds.map(build => config.dates[build]),
      datasets: keys.map(function (key){
          return {
              label: key,
              data: builds.map(build => display_value(key, config.result[build])),
              borderColor: random_color(key),
              backgroundColor: 'rgba(0, 0, 0, 0)',
              hidden: !visible_keys.has(key),
          }
      })
    };
}

function fetchUpdateChart() {
  var chart_spinner = document.getElementById('chart_spinner');
  chart_spinner.style.visibility = 'visible';
  fetch_params = compute_fetch_params();
  console.log('fetch')
  // a newer request replaces the previous one, which could be answered last
  statsRequest?.abort();
  const request = new AbortController();
  statsRequest = request;
  fetchStats('/runbot/stats/', fetch_params, request.signal).then(function(result) {
    if (request.signal.aborted) {
      return;
    }
    chart_spinner.style.visibility = 'hidden';
    if (result) {
      config.result = result['stats'];
      config.dates = result['dates'];
      // ordered by batch, unlike the keys of the stats; as strings, like the URL params
      config.builds = result['builds'].map(String);
      Object.values(config.result).forEach((v) => {
        const values = Object.values(v);
        const sum = values.reduce((a, b) => a + b, 0);
        v['Aggregate Sum'] = sum;
        v['Aggregate Average'] = sum / values.length;
      });
    } else {
      // the previous stats don't match the new params
      config.result = {};
      config.dates = {};
      config.builds = [];
    }
    updateChart()
  });
}

function generateLegend() {
    const legend = document.createElement("ul");
    for (const data of config.data.datasets) {
        const legendElement = document.createElement("li");
        const color = document.createElement("span");
        color.className = "color";
        color.style.border = `2px solid ${data.borderColor}`;
        const label = document.createElement("span");
        label.className = "label";
        label.title = data.label;
        label.textContent = data.label;
        legendElement.append(color, label);
        if (data.hidden){
            legendElement.classList.add("disabled");
        }
        legend.append(legendElement);
    }
    document.getElementById("js-legend").replaceChildren(legend);
    document.querySelectorAll("#js-legend > ul > li").forEach((element, index) => {
        element.addEventListener("click", () => {
            const curr = window.statsChart.data.datasets[index];
            curr.hidden = !curr.hidden;
            config.searchParams.nb_dataset=-1;
            config.searchParams.visible_keys = window.statsChart.data.datasets.filter((dataset) => !dataset.hidden).map((dataset) => encodeURIComponent(dataset.label)).join(",");
            updateChart();
        });
    });
}

function updateForm() {
  for([key, value] of Object.entries(config.searchParams)){
    var selector = document.getElementById(key + '_selector');
    if (selector != null){
      selector.value = value;
      selector.onchange = function(){
        var id = this.id.replace('_selector', '');
        config.searchParams[id] = this.value;
        const categoryChanged = id == 'key_category' || (id == 'trigger_id' && updateCategory());
        if (id == 'trigger_id' || id == 'key_category') {
          updateStep();
        }
        if (categoryChanged && config.searchParams.nb_dataset == -1) {
          // the custom selection is made of the keys of the previous category, back to the default of the template
          config.searchParams.nb_dataset = parseInt(document.querySelector('#nb_dataset_selector option[selected]').value, 10);
          config.searchParams.visible_keys = '';
        }
        if (id == 'display_aggregate' && this.value != 'none' && config.searchParams.nb_dataset == -1) {
          // show the chosen aggregate in the custom selection
          const visibleKeys = new Set(config.searchParams.visible_keys.split(',').filter(Boolean));
          visibleKeys.add(encodeURIComponent(aggregateKeys[this.value]));
          config.searchParams.visible_keys = [...visibleKeys].join(',');
        }
        if (localParams.indexOf(id) == -1){
          fetchUpdateChart();
        } else {
          updateChart()
        }
      }
    }
  }
  const builds = config.builds || [];
  // fast forward stays available without builds, to leave an empty centered range
  const enableFastForward = config.searchParams.center_build_id != 0 && (config.searchParams.center_build_id !== builds.slice(-1)[0]);
  const enableForward = builds.length > 0 && enableFastForward;
  document.getElementById("forward_button").disabled = !enableForward;
  document.getElementById("fast_forward_button").disabled = !enableFastForward;
  const enableBackward = builds.length > 0 && (config.searchParams.center_build_id !== builds[0]);
  document.getElementById("backward_button").disabled = !enableBackward;
}

function updateChart(){
  updateForm()
  updateUrl();
  process_chart_data();    
  if (! window.statsChart) {
    var ctx = document.getElementById('canvas').getContext('2d');
    window.statsChart = new Chart(ctx, config);
  } else {
    window.statsChart.update();
  }
  generateLegend();
}

function updateCategory() {
  // only the categories of the selected trigger can be chosen
  const options = [...document.getElementById('key_category_selector').options];
  for (const option of options) {
    option.disabled = !option.dataset.triggers.split(',').includes(String(config.searchParams.trigger_id));
  }
  if (options.some((option) => !option.disabled && option.value === config.searchParams.key_category)) {
    return false;
  }
  config.searchParams.key_category = options.find((option) => !option.disabled)?.value || '';
  return true;
}

function updateStep() {
  // only the steps of the selected trigger and category can be chosen
  const scope = `${config.searchParams.trigger_id}:${config.searchParams.key_category}`;
  const options = [...document.getElementById('key_step_selector').options];
  for (const option of options) {
    option.disabled = option.value !== '' && !option.dataset.scopes.split(',').includes(scope);
  }
  if (options.find((option) => option.value === config.searchParams.key_step)?.disabled) {
    config.searchParams.key_step = '';
  }
}

function compute_fetch_params(){
  return {
    ...config.searchParams,
    bundle_id: document.getElementById('bundle_id').value,
  }
};

function updateUrl(){
  window.location.hash = new URLSearchParams(config.searchParams).toString();
}

async function waitForChart() {

  function loop(resolve) {
    if (window.Chart) {
      resolve();
    } else {
      setTimeout(loop.bind(null, resolve),10);
    }
  }
  return new Promise((resolve) => {
    loop(resolve);
  })
}

window.onload = function() {
    config.searchParams = {
      trigger_id: main_trigger,
      key_step: '',
      limit: 25,
      center_build_id: 0,
      // preselected by the template, among the categories of the bundle
      key_category: document.getElementById('key_category_selector').value,
      mode: 'normal',
      nb_dataset: parseInt(document.getElementById('nb_dataset_selector').value, 10),
      display_aggregate: 'none', 
      visible_keys: '',
    };
    localParams = ['display_aggregate', 'mode', 'nb_dataset', 'visible_keys']
  
    for([key, value] of new URLSearchParams(window.location.hash.replace("#","?"))){
      config.searchParams[key] = value;
    }
    updateCategory();
    updateStep();

    document.getElementById('backward_button').onclick = function(){
      config.searchParams['center_build_id'] = config.builds[0];
      fetchUpdateChart();
    }
    document.getElementById('forward_button').onclick = function(){
      config.searchParams['center_build_id'] = config.builds.slice(-1)[0];
      fetchUpdateChart();
    }
    document.getElementById('fast_forward_button').onclick = function(){
      config.searchParams['center_build_id'] = 0;
      fetchUpdateChart();
    }

    waitForChart().then(fetchUpdateChart);
};
