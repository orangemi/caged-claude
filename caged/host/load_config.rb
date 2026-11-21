require 'yaml'
require 'json'

begin
  source, action, selected = ARGV
  text = File.read(source)
  # The keyword API differs between the Ruby versions shipped with macOS.
  if RUBY_VERSION.split('.').first.to_i >= 3
    config = YAML.safe_load(text, permitted_classes: [], aliases: true)
  else
    config = YAML.safe_load(text, [], [], true)
  end
  abort 'Configuration must be a YAML mapping.' unless config.is_a?(Hash)
  if action == 'read'
    puts JSON.generate(config)
    exit
  end
  nodes = config.fetch('proxies', [])
  if action == 'list'
    result = nodes.map { |node| { name: node['name'], type: node['type'], udp: node['udp'] } }
  else
    result = nodes.find { |node| node['name'] == selected }
    abort 'Selected node was not found in the proxy configuration.' unless result
    abort 'This first version supports standalone AnyTLS nodes only.' unless result['type'] == 'anytls'
    abort 'A chained node must be expanded before it can be used independently.' if result['dialer-proxy']
  end
  puts JSON.generate(result)
rescue Psych::Exception, Errno::ENOENT, KeyError
  abort 'Cannot read the YAML configuration. No configuration contents were printed.'
end
