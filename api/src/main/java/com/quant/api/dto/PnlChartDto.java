package com.quant.api.dto;

import com.quant.api.domain.PortfolioSnapshot;
import lombok.Builder;
import lombok.Getter;

import java.math.BigDecimal;
import java.time.LocalDate;

@Getter
@Builder
public class PnlChartDto {
    private LocalDate date;
    private BigDecimal totalValue;
    private BigDecimal cumulativePnl;
    private BigDecimal dailyPnl;

    public static PnlChartDto from(PortfolioSnapshot s) {
        return PnlChartDto.builder()
            .date(s.getSnapshotDate())
            .totalValue(s.getTotalValue())
            .cumulativePnl(s.getCumulativePnl())
            .dailyPnl(s.getRealizedPnl())
            .build();
    }
}
