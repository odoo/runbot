
var config = {
  type: 'line',
  options: {
    legend: {
        display: true,
        position: 'right',
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
          labelString: 'Queries'
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
          config.searchParams['max_build_id'] = build_id;
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

function random_color(module_name){
    var colors = ['#004acd', '#3658c3', '#4a66ba', '#5974b2', '#6581aa', '#6f8fa3', '#7a9c9d', '#85a899', '#91b596', '#a0c096', '#fdaf56', '#f89a59', '#f1865a', '#e87359', '#dc6158', '#ce5055', '#bf4150', '#ad344b', '#992a45', '#84243d'];
    var sum = 0;
    for (var i = 0; i < module_name.length; i++) {
        sum += module_name.charCodeAt(i);
    }
    sum = sum % colors.length;
    color = colors[sum];

    return color
};


function process_chart_data(){
    if (Object.keys(config.result).length == 0)
    {
      config.data = {
        labels:[],
        datasets: [],
      }
      return
    }
    var builds = Object.keys(config.result);
    var newer_build_stats = config.result[builds[0]];
    var older_build_stats = config.result[builds.slice(-1)[0]];

    var mode = document.getElementById('mode_selector').value;

    function display_value(module, build_stats){
        // {'base': 50, 'crm': 25 ...}
        if (build_stats === undefined)
            build_stats = newer_build_stats;
        if (build_stats[module] === undefined)
            return NaN;
        if (mode == 'normal')
            return build_stats[module]
        if (older_build_stats[module] === undefined)
            return NaN;
        return build_stats[module] - older_build_stats[module]
    }

    var modules = Object.keys(newer_build_stats);

    modules.sort((m1, m2) => Math.abs(display_value(m2)) - Math.abs(display_value(m1)));
    console.log(config.searchParams.nb_dataset)
    modules = modules.slice(0, config.searchParams.nb_dataset);

    config.data = {
        labels: builds,
        datasets: modules.map(function (key){
            return {
                label: key,
                data: builds.map(build => display_value(key, config.result[build])),
                borderColor: random_color(key),
                backgroundColor: 'rgba(0, 0, 0, 0)',
                lineTension: 0
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
    chart_spinner.style.visibility = 'hidden';
    updateChart()
  });
};

function updateChart(){
  updateUrl();
  process_chart_data();    
  if (! window.statsChart) {
    var ctx = document.getElementById('canvas').getContext('2d');
    window.statsChart = new Chart(ctx, config);
  } else {
    window.statsChart.update();
  }
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

window.onload = function() {

    var mode_selector = document.getElementById('mode_selector');
    var fast_backward_button = document.getElementById('fast_backward_button');

    config.searchParams = {
      limit: 25,
      max_build_id: 0,
      key_category: 'module_loading_queries',
      mode: 'normal',
      nb_dataset: 20,
    };
    localParams = ['mode', 'nb_dataset']
  
    for([key, value] of new URLSearchParams(window.location.hash.replace("#","?"))){
      config.searchParams[key] = value;
    }


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

    fast_backward_button.onclick = function(){
      config.searchParams['max_build_id'] = Object.keys(config.result)[0];
      fetchUpdateChart();
    }

    fetchUpdateChart();
};
