package com.quant.api.repository;

import com.quant.api.domain.Trade;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

import java.math.BigDecimal;
import java.time.OffsetDateTime;
import java.util.List;

public interface TradeRepository extends JpaRepository<Trade, Long> {

    Page<Trade> findByModeOrderByTradedAtDesc(String mode, Pageable pageable);

    Page<Trade> findByMarketAndModeOrderByTradedAtDesc(String market, String mode, Pageable pageable);

    @Query("""
        SELECT COALESCE(SUM(t.realizedPnl), 0)
        FROM Trade t
        WHERE t.side = 'SELL' AND t.mode = :mode
        """)
    BigDecimal sumRealizedPnl(String mode);

    @Query("""
        SELECT t FROM Trade t
        WHERE t.mode = :mode AND t.tradedAt >= :since
        ORDER BY t.tradedAt DESC
        """)
    List<Trade> findRecentByMode(String mode, OffsetDateTime since);

    @Query("""
        SELECT COUNT(t) FROM Trade t
        WHERE t.side = 'SELL' AND t.mode = :mode AND t.realizedPnl > 0
        """)
    long countWinningTrades(String mode);

    @Query("""
        SELECT COUNT(t) FROM Trade t
        WHERE t.side = 'SELL' AND t.mode = :mode
        """)
    long countClosedTrades(String mode);
}
