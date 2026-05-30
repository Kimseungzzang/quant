package com.quant.api.controller;

import com.quant.api.dto.PnlChartDto;
import com.quant.api.dto.PnlSummaryDto;
import com.quant.api.dto.TradeDto;
import com.quant.api.service.TradeService;
import lombok.RequiredArgsConstructor;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Sort;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/trades")
@RequiredArgsConstructor
@CrossOrigin(origins = {"http://localhost:3000"})
public class TradeController {

    private final TradeService tradeService;

    @GetMapping
    public Page<TradeDto> getTrades(
        @RequestParam(defaultValue = "paper") String mode,
        @RequestParam(required = false) String market,
        @RequestParam(defaultValue = "0") int page,
        @RequestParam(defaultValue = "20") int size
    ) {
        return tradeService.getTrades(mode, market, PageRequest.of(page, size));
    }

    @GetMapping("/pnl/summary")
    public PnlSummaryDto getPnlSummary(
        @RequestParam(defaultValue = "paper") String mode
    ) {
        return tradeService.getPnlSummary(mode);
    }

    @GetMapping("/pnl/chart")
    public List<PnlChartDto> getPnlChart(
        @RequestParam(defaultValue = "paper") String mode,
        @RequestParam(defaultValue = "30") int days
    ) {
        return tradeService.getPnlChart(mode, days);
    }
}
