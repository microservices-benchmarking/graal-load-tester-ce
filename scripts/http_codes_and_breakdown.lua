local threads = {}
local request_times_format = ";%s,%s"

-- This code is specific to LuaJIT
local ffi = require("ffi")

ffi.cdef[[
    struct timeval {
        long tv_sec;
        long tv_usec;
    };
    int gettimeofday(struct timeval *tv, void *tz);
]]

function setup(thread)
   table.insert(threads, thread)
end

function init(args)
   requests  = 0
   responses = 0
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

   first_request = 0
   total_times = {}
   node_times = {}
   timestamps = {}
end

local function print_percentile(sorted, percentile_str, percentile)
   -- Prints the breakdown of the given percentile given a sorted list of requests
   local percentile_status = "   - %7.3f: %8.2f %8.2f %8.2f"
   local n = #sorted
   if n == 0 then return 0 end

   local rank = percentile * (n - 1) + 1
   local lower = math.floor(rank)
   local upper = math.ceil(rank)
   local weight = rank - lower

   if upper > n then
      print(percentile_status:format(
         percentile_str,
         sorted[n].total_time,
         sorted[n].network_time,
         sorted[n].node_time
      ))
   else
      print(percentile_status:format(
         percentile_str,
         sorted[lower].total_time * (1 - weight) + sorted[upper].total_time * weight,
         sorted[lower].network_time * (1 - weight) + sorted[upper].network_time * weight,
         sorted[lower].node_time * (1 - weight) + sorted[upper].node_time * weight
      ))
   end
end

function now_ms()
   -- Custom function that gets the current time in ms
   local tv = ffi.new("struct timeval")
   ffi.C.gettimeofday(tv, nil)
   local high_res_time = tonumber(tv.tv_sec) + (tonumber(tv.tv_usec) / 1000000)
   return high_res_time * 1000.0
end

function request()
   requests = requests + 1
   -- We set the header, to measure when the request was sent
   --  - if the app supports the breakdown, it will be sent back
   wrk.headers["X-WRK-Request-Start"] = tostring(now_ms())
   return wrk.format(nil, wrk.path)
end

function response(status, headers, body)
   responses = responses + 1
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

   -- Get the headers
   local request_start = headers["X-WRK-Request-Start"]
   local nginx_start = headers["X-OCI-GRAAL-Request-Start"]
   local response_start = headers["X-Response-Start"]

   if request_start and nginx_start and response_start then
      local request_start_time = tonumber(request_start)
      -- We save timestamps in order to detect thread calibration phase
      --  - There does not seem to be better way to detect the thread calibration
      --  - The length of the thread calibration is dependent on the number of threads, and should be:
      --      thread_calibration = 10s + #threads * 5ms
      local response_end = now_ms()
      local nginx_start_time = tonumber(nginx_start) * 1000.0
      local response_start_time = tonumber(response_start)
      table.insert(timestamps, request_start_time)
      table.insert(total_times, response_end - request_start_time)
      table.insert(node_times, response_start_time - nginx_start_time)
   end
end

function done(summary, latency, requests)
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

   all_responses = {}
   responses = {}

   for index, thread in ipairs(threads) do
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

      -- Parse the breakdowns
      total_times = thread:get("total_times")
      node_times = thread:get("node_times")
      timestamps = thread:get("timestamps")
      for i = 1, math.min(#total_times, #node_times) do
         total_time = total_times[i]
         node_time = node_times[i]
         -- Guard against negative time
         --   - the issue here is that we are mixing timestamps with different precision, and different granurality
         --   - these can have slight differences due to rounding/truncating/precision resulting into small <1ms difference.
         --   - for very small values this can lead to negative values.
         local network_time = math.max(total_time - node_time, 0)
         table.insert(all_responses, {
            start_ts = timestamps[i],
            network_time = network_time,
            node_time = node_time,
            total_time = total_time
         })
      end
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

   -- If there was breakdown
   if next(all_responses) ~= nil then
      -- We first drop the packets from the thread calibration
      --   - we are doing this after it is done, so little inefficiency is acceptable
      --   - since we might use older lua, we have to drop one by one
      --   - using new table with explicit indexing seems to be most efficient
      table.sort(all_responses, function(a, b)
         return a.start_ts < b.start_ts
      end)
      local thread_calibration_size = #all_responses - #latency
      for i = thread_calibration_size + 1, #all_responses do
         responses[#responses+1] = all_responses[i]
      end
      print("------------------------------\n")
      print("  Latency breakdown (node and network time)")
      print("                 Total  Network     Node")
      table.sort(responses, function(a, b)
         return a.total_time < b.total_time
      end)
      print_percentile(responses, "50", 0.50)
      print_percentile(responses, "75", 0.75)
      print_percentile(responses, "90", 0.90)
      print_percentile(responses, "99", 0.99)
      print_percentile(responses, "99.9", 0.999)
      print_percentile(responses, "99.99", 0.9999)
      print_percentile(responses, "99.999", 0.99999)
      print_percentile(responses, "100", 1.0)
   end
end
