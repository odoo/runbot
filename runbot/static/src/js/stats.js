
var config = {
  type: 'line',
  options: {

    animation: {
      duration: 0
    },
    legend: {
        display: false,
    },
    responsive: true,
    tooltips: {
      mode: 'point'
    },
    scales: {
      xAxes: [{
        display: true,
        scaleLabel: {
          display: true,
          labelString: 'Builds'
        }
      }],
      yAxes: [{
        display: true,
        scaleLabel: {
          display: true,
          labelString: 'Value'
        },
      }]
    }
  }
};

config.options.onClick = function(event, activeElements) {
    if (activeElements.length === 0){
        var x_label_index = this.scales['x-axis-0'].getValueForPixel(event.x);
        var build_id = config.data.labels[x_label_index]
        if (event.layerY > this.chartArea.bottom && event.layerY < this.chartArea.bottom + this.scales['x-axis-0'].height){
          config.searchParams['center_build_id'] = build_id;
          fetchUpdateChart();
        }
        return;
    }
    window.open('/runbot/build/stats/' + config.data.labels[activeElements[0]._index]);
};

function fetch(path, data, then) {
  const xhttp = new XMLHttpRequest();
  xhttp.onreadystatechange = function() {
      if (this.readyState == 4 && this.status == 200) {
          const res = JSON.parse(this.responseText);
          then(res.result);
      }
  };
  xhttp.open("POST", path);
  xhttp.setRequestHeader('Content-Type', 'application/json');
  xhttp.send(JSON.stringify({params:data}));
};

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


    var builds = Object.keys(config.result);
    var newer_build_stats = config.result[builds.slice(-1)[0]];
    var older_build_stats = config.result[builds[0]];
    var keys = Object.keys(newer_build_stats) ;
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
        sort_value = newer_build_stats[key]
      } else if (mode == 'alpha') {
        sort_value = key
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
          sort_value = Math.abs(newer_build_stats[key] - previous_value)
        }
      }
      sort_values[key] = sort_value
    }
    keys.sort((m1, m2) => sort_values[m2] - sort_values[m1]);

    if (config.searchParams.nb_dataset != -1) {
      visible_keys = new Set(keys.slice(0, config.searchParams.nb_dataset));
    } else {
      visible_keys = new Set(config.searchParams.visible_keys.split('-'))
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
      labels: builds,
      datasets: keys.map(function (key){
          return {
              label: key,
              data: builds.map(build => display_value(key, config.result[build])),
              borderColor: random_color(key),
              backgroundColor: 'rgba(0, 0, 0, 0)',
              lineTension: 0,
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
  fetch('/runbot/stats/', fetch_params, function(result) {
    config.result = result;
    Object.values(config.result).forEach(v => v['Aggregate Sum'] = Object.values(v).reduce((a, b) => a + b, 0))
    Object.values(config.result).forEach(v => v['Aggregate Average'] = Object.values(v).reduce((a, b) => a + b, 0)/Object.values(v).length)
    chart_spinner.style.visibility = 'hidden';
    updateChart()
  });
}

function generateLegend() {
  var legend = $("<ul></ul>");
  for (data of config.data.datasets) {
    var legendElement = $(`<li><span class="color" style="border: 2px solid ${data.borderColor};"></span><span class="label" title="${data.label}">${data.label}<span></li>`)
    if (data.hidden){
      legendElement.addClass('disabled')
    }
    legend.append(legendElement)
  }
  $("#js-legend").html(legend);
  $("#js-legend > ul > li").on("click",function(e){
    var index = $(this).index();
    //$(this).toggleClass("disabled")
    var curr = window.statsChart.data.datasets[index];
    curr.hidden = !curr.hidden;
    config.searchParams.nb_dataset=-1; 
    config.searchParams.visible_keys = window.statsChart.data.datasets.filter(dataset => !dataset.hidden).map(dataset => dataset.label).join('-')
    updateChart();
  })
}

function updateForm() {
  for([key, value] of Object.entries(config.searchParams)){
    var selector = document.getElementById(key + '_selector');
    if (selector != null){
      selector.value = value;
      selector.onchange = function(){
        var id = this.id.replace('_selector', '');
        config.searchParams[this.id.replace('_selector', '')] = this.value;
        if (localParams.indexOf(id) == -1){
          fetchUpdateChart();
        } else {
          updateChart()
        }
      }
    }
  }
  let display_forward = config.result && config.searchParams.center_build_id != 0 && (config.searchParams.center_build_id !== Object.keys(config.result).slice(-1)[0])
  document.getElementById("forward_button").style.visibility = display_forward ? "visible":"hidden";
  document.getElementById("fast_forward_button").style.visibility = display_forward ? "visible":"hidden";
  let display_backward = config.result && (config.searchParams.center_build_id !== Object.keys(config.result)[0])
  document.getElementById("backward_button").style.visibility = display_backward ? "visible":"hidden";
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

function compute_fetch_params(){
  return {
    ...config.searchParams,
    bundle_id: document.getElementById('bundle_id').value,
    trigger_id: document.getElementById('trigger_id').value,
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
      limit: 25,
      center_build_id: 0,
      key_category: 'module_loading_queries',
      mode: 'normal',
      nb_dataset: 20,
      display_aggregate: 'none', 
      visible_keys: '',
    };
    localParams = ['display_aggregate', 'mode', 'nb_dataset', 'visible_keys']
  
    for([key, value] of new URLSearchParams(window.location.hash.replace("#","?"))){
      config.searchParams[key] = value;
    }

    document.getElementById('backward_button').onclick = function(){
      config.searchParams['center_build_id'] = Object.keys(config.result)[0];
      fetchUpdateChart();
    }
    document.getElementById('forward_button').onclick = function(){
      config.searchParams['center_build_id'] = Object.keys(config.result).slice(-1)[0];
      fetchUpdateChart();
    }
    document.getElementById('fast_forward_button').onclick = function(){
      config.searchParams['center_build_id'] = 0;
      fetchUpdateChart();
    }

    waitForChart().then(fetchUpdateChart);
};
