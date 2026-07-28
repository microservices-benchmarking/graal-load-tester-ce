-- This script will apply load in parallel to a set of URLs defined in the neighboring 'load-generator-urls.lua' script.
-- Each thread will open connections to one of the URLs in the list in a way.
-- NOTE: The number of threads used for the experiment must be a multiple of the the number of URLs to evenly distribute the load.
-- NOTE: If the number of threads is smaller than the list of URLs, the extra URLs will be ignored.


-- The require command will import 'load-generator-urls.lua' from the current directory.
--  All its content will be set in the current scope.

-- It must set a 'urls' table for the parallel lua script to work.
--
-- Example:
--           urls = {"https://www.graalvm.org", "https://example.com/foo"}


-- the sorcery below adds the script directory to the search path for imports because there is no relative imports by default
function addRelPath(dir)
  local spath =
      debug.getinfo(1,'S').source
        :sub(2)
        :gsub("^([^/])","./%1")
        :gsub("[^/]*$","")
  dir = dir and (dir.."/") or ""
  spath = spath..dir
  package.path = spath.."?.lua;"
               ..spath.."?/init.lua"
               ..package.path
end
addRelPath()


-- abort early if load-generator-urls.lua doesn't exist or has parsing errors
local import_success, require_error_message = pcall(require, "load-generator-urls")
if not import_success then
   print("Couldn't import 'load-generator-urls.lua' !")
   print("ERROR", require_error_message)
   os.exit(1)
end
require "load-generator-urls"; --  if used with the gos_harness, this file will be automatically created and deleted


function var_exists(var_name)
  -- checks if an variable name is present in the environment
  for k, _ in pairs(_G) do
    if k == var_name then
      return true
    end
  end
  return false
end

function parse_link(url)
   local protocol, remainder = string.match(url, "^(%w+)://(.+)$")
   if protocol == nil then
      return nil, nil, nil, nil
   end

   local host_and_port, path = string.match(remainder, "^([^/]+)(/.*)$")
   if host_and_port == nil then
      host_and_port = remainder
      path = "/"
   end

   local host, port = string.match(host_and_port, "^([^:]+):(%d+)$")
   if host == nil then
      host = host_and_port
   end

   return protocol, host, path, port
end


function get_addr(protocol, url, port)
   if url == nil then
      print("Cannot resolve 'nil' URL !")
      os.exit(1)
   end

   -- wrk.lookup behaves like getaddrinfo() on Linux. It may return several IPs.
   -- we should test them and remove the ones we cannot connect to.
   local service = port or protocol or "http"
   addrs = wrk.lookup(url, service)
   for i = #addrs, 1, -1 do
      if not wrk.connect(addrs[i]) then
         table.remove(addrs, i)
      end
   end
   if #addrs == 0 then
      print("Couldn't connect to", url)
      os.exit(1)
   end
   return addrs[1]
end


-- special wrk function called for each thread setup
-- The number of calls corresponds to the '-t' option on the command line
local thread_ids = {}
local threads = {}
function setup(thread)
   if not var_exists("urls") then
      print("'urls' table must be set in the imported script ! Aborting...")
      print("Example:\n\turls = {\"https://www.graalvm.org\", \"https://example.com\"}")
      os.exit(1)
   end
   -- store each thread globally
   table.insert(threads, thread)
   -- set a unique 'thread_id' value in each thread. Goes from 1 to N
   table.insert(thread_ids, #thread_ids)
   thread:set("thread_id", #thread_ids)

   target_url = urls[((thread:get("thread_id")-1) % #urls)+1]
   a_protocol, a_url, a_path, a_port = parse_link(target_url)
   thread:set("target_url", a_url)
   thread:set("target_path", a_path)

   address = get_addr(a_protocol, a_url, a_port)
   print("Thread config: ", thread:get("thread_id"), address, target_url)
   
   -- Modifying wrk target server
   thread.addr = address
end


-- special wrk function called for each request call to format the HTTP request
function request()
   -- create the HTTP request using the custom path that got parsed during setup
   return wrk.format(nil, target_path)
end


-- special wrk function called once for each thread. Called after setup(thread).
-- this is too late to connect to a different server here since the connection happens
-- after setup and before init.
function init(args)
   -- 'args' is the list of command line arguments passed after '--'
   response_ctr = 0
   count_2xx = 0
   count_3xx = 0
   count_400 = 0
   count_401 = 0
   count_404 = 0
   count_429 = 0
   count_4xx = 0
   count_500 = 0
   count_502 = 0
   count_503 = 0
   count_504 = 0
   count_5xx = 0
   count_xxx = 0
end

-- special wrk function called to process every response
-- It's expensive to fully parse the output, so this should only be added for debugging purposes
function response(status, headers, body)
     response_ctr = response_ctr + 1
     local nstatus = tonumber(status)
     if nstatus < 300 then      count_2xx = count_2xx + 1
     elseif nstatus < 400 then  count_3xx = count_3xx + 1
     elseif nstatus == 400 then count_400 = count_400 + 1
     elseif nstatus == 401 then count_401 = count_401 + 1
     elseif nstatus == 404 then count_404 = count_404 + 1
     elseif nstatus == 429 then count_429 = count_429 + 1
     elseif nstatus < 500 then  count_4xx = count_4xx + 1
     elseif nstatus == 500 then count_500 = count_500 + 1
     elseif nstatus == 502 then count_502 = count_502 + 1
     elseif nstatus == 503 then count_503 = count_503 + 1
     elseif nstatus == 504 then count_504 = count_504 + 1
     elseif nstatus < 600 then  count_5xx = count_5xx + 1
     else count_xxx = count_xxx + 1
     end
--   print("thread id:", thread_id, target_url)
--   print(status)
--   print(body)
end

-- special wrk function called once before the process completes. It contains raw performance numbers
function done(summary, latency, requests)
  -- no proper documentation apart from the 'summary' value
  -- but here are a few pointers:
  --    - https://github.com/wg/wrk/issues/134
  --    - https://github.com/wg/wrk/issues/431#issuecomment-692581251
  total_2xx = 0
  total_3xx = 0
  total_400 = 0
  total_401 = 0
  total_404 = 0
  total_429 = 0
  total_4xx = 0
  total_500 = 0
  total_502 = 0
  total_503 = 0
  total_504 = 0
  total_5xx = 0
  total_xxx = 0

  for _, thread in ipairs(threads) do
    total_2xx = total_2xx + thread:get("count_2xx")
    total_3xx = total_3xx + thread:get("count_3xx")
    total_400 = total_400 + thread:get("count_400")
    total_401 = total_401 + thread:get("count_401")
    total_404 = total_404 + thread:get("count_404")
    total_429 = total_429 + thread:get("count_429")
    total_4xx = total_4xx + thread:get("count_4xx")
    total_500 = total_500 + thread:get("count_500")
    total_502 = total_502 + thread:get("count_502")
    total_503 = total_503 + thread:get("count_503")
    total_504 = total_504 + thread:get("count_504")
    total_5xx = total_5xx + thread:get("count_5xx")
    total_xxx = total_xxx + thread:get("count_xxx")
  end

  print("------------------------------\n")
  local msg_status = "HTTP status %s count: %d"
  print(msg_status:format("2xx", total_2xx))
  print(msg_status:format("3xx", total_3xx))
  print(msg_status:format("400", total_400))
  print(msg_status:format("401", total_401))
  print(msg_status:format("404", total_404))
  print(msg_status:format("429", total_429))
  print(msg_status:format("4xx", total_4xx))
  print(msg_status:format("500", total_500))
  print(msg_status:format("502", total_502))
  print(msg_status:format("503", total_503))
  print(msg_status:format("504", total_504))
  print(msg_status:format("5xx", total_5xx))
  print(msg_status:format("xxx", total_xxx))

  for idx, thread in ipairs(threads) do
    local thread_throughput = thread:get("response_ctr") / (summary.duration / 1000000)
    local url = urls[((idx-1) % #urls)+1]
    print(string.format("Thread %d throughput is %.3f reqs/s for endpoint %s", idx, thread_throughput, url))
  end
end
