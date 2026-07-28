local threads = {}

function setup(thread)
   table.insert(threads, thread)
end

function init(args)
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

function request()
   return wrk.format(nil, wrk.path)
end

function response(status, headers, body)
   local nstatus = tonumber(status)
   if nstatus == nil then      count_xxx = count_xxx + 1
   elseif nstatus < 300 then   count_2xx = count_2xx + 1
   elseif nstatus < 400 then   count_3xx = count_3xx + 1
   elseif nstatus == 400 then  count_400 = count_400 + 1
   elseif nstatus == 401 then  count_401 = count_401 + 1
   elseif nstatus == 404 then  count_404 = count_404 + 1
   elseif nstatus == 429 then  count_429 = count_429 + 1
   elseif nstatus < 500 then   count_4xx = count_4xx + 1
   elseif nstatus == 500 then  count_500 = count_500 + 1
   elseif nstatus == 502 then  count_502 = count_502 + 1
   elseif nstatus == 503 then  count_503 = count_503 + 1
   elseif nstatus == 504 then  count_504 = count_504 + 1
   elseif nstatus < 600 then   count_5xx = count_5xx + 1
   else count_xxx = count_xxx + 1
   end
end

function done(summary, latency, requests)
   local total_2xx = 0
   local total_3xx = 0
   local total_400 = 0
   local total_401 = 0
   local total_404 = 0
   local total_429 = 0
   local total_4xx = 0
   local total_500 = 0
   local total_502 = 0
   local total_503 = 0
   local total_504 = 0
   local total_5xx = 0
   local total_xxx = 0

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
end
